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
import tarfile
import tempfile
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
    # Cuanto se espera a que cloud-init acabe de instalar las herramientas antes
    # de dar el arranque por perdido. Medido el 2026-09-11 en dos droplets:
    # 272 s y 348 s desde el arranque. El techo es muy superior a proposito: la
    # parte lenta es apt, y su duracion no depende de nosotros -si `apt-daily`
    # coge el cerrojo de dpkg, se espera lo que haga falta-.
    # El 2026-09-10 por la noche, con 900 s, dos `launch dev` seguidos murieron
    # ahi. Esperar de mas cuesta centimos; relanzar cuesta el lanzamiento entero.
    "DO_DEV_TOOLS_TIMEOUT": "1800",
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

# A partir de aquí, un arranque que acabe bien se denuncia igualmente. Subir el
# plazo de espera de 900 a 1800 s dejó de perder lanzamientos, pero de paso
# convertía el arranque patológico en un ÉXITO MUDO: antes fallaba y al menos se
# notaba. Medido el 2026-09-11 en dos droplets: 272 s y 348 s. 600 s es el doble
# de lo peor visto, así que pasar de ahí no es "iba un poco lento".
LENTO_SOSPECHOSO = 600

# Sondas seguidas con la clave rechazada antes de dar el acceso por imposible.
# No es 1 porque la primera puede caer mientras sshd aun se esta colocando; no es
# 20 porque un rechazo de clave NO se arregla esperando: el droplet solo acepta
# las claves registradas cuando se creo, y eso ya no cambia. Con la sonda cada
# 10 s, seis son ~1 minuto: suficiente para descartar el arranque y muy lejos de
# los 30 del plazo, que es lo que se perdia antes en silencio.
RECHAZOS_FATALES = 6


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


# Nombre de la clave compartida por toda la flota en la cuenta de DigitalOcean, y
# fichero donde vive. UNA clave registrada UNA vez, en vez de una por maquina: el
# acceso entre maquinas dependia del ORDEN DE NACIMIENTO, y eso no se arregla con
# mas claves sino con una que exista antes que todas. Medido el 2026-09-10: el
# mini tenia tres claves autorizadas y ninguna era del dev, porque la del dev se
# registra durante SU provision, que es siempre despues de que el mini exista.
NOMBRE_CLAVE_FLOTA = "flota"
FICHERO_CLAVE_FLOTA = "~/.ssh/do_flota"


def ruta_clave_flota() -> Path:
    return Path(cfg("DO_FLEET_KEY_FILE") or FICHERO_CLAVE_FLOTA).expanduser()


def ruta_publica(privada: Path) -> Path:
    """La .pub de una clave. Aparte porque `with_suffix` NO vale aqui.

    `Path("~/.ssh/do_flota").with_suffix(".pub")` da lo que uno espera, pero con
    un fichero como `id_ed25519.old` se comeria el `.old`. Concatenar es lo que
    hace ssh-keygen y lo unico que no sorprende.
    """
    return Path(str(privada) + ".pub")


# Se avisa UNA vez por proceso de que se cayo a la clave de la flota. Sin el
# flag, un `launch` suelta la misma linea en cada sonda de la espera.
_aviso_caida_a_flota = False


def fichero_clave_ssh() -> Path:
    """La clave privada con la que se entra en los droplets.

    Devuelve la de `DO_SSH_KEY_FILE` **si existe**; si no existe y la de la
    flota si, devuelve esa.

    Por que esa caida no es un apano: `DO_SSH_KEY_FILE` no lo declara NADA del
    repo -no lo pide ningun tipo, ni el llavero, ni .env.example-, sino que lo
    escribe `_mandar_clave_flota()` dentro de `dev-secrets.env`. O sea que solo
    llega por el ENTORNO, y el entorno de un servicio es una FOTO de cuando
    arranco: el bot de una maquina que nacio antes de esa linea no la ve nunca.
    Entonces `cfg()` devuelve el defecto `~/.ssh/do_droplet`, **que en esa
    maquina no existe**, y el `launch` se queda sondeando un droplet vivo y
    facturando con una clave inexistente hasta que la espera lo declara
    rechazado. Desde una sesion SSH el mismo comando funciona, porque un shell
    de login si lee el fichero: la misma trampa del 2026-09-11 en el mini, por
    la otra punta.
    Medido el 2026-09-11: `launch mini` desde el bot de un dev murio con
    `root@161.35.50.148: Permission denied (publickey)` intentando
    `/home/deploy/.ssh/do_droplet`, con la clave de la flota buena al lado.

    Y la eleccion es segura, no una adivinanza: la de flota esta registrada en
    la cuenta por definicion -`clave-flota` la registra- y `DO_SSH_KEYS` vacio
    mete TODAS las de la cuenta en cada droplet nuevo, asi que es una clave que
    el droplet de enfrente acepta seguro. La alternativa era un fichero que no
    existe, que no puede autenticar nada.
    """
    global _aviso_caida_a_flota
    configurada = Path(cfg("DO_SSH_KEY_FILE")).expanduser()
    if configurada.exists():
        return configurada
    flota = ruta_clave_flota()
    if not flota.exists():
        return configurada
    if not _aviso_caida_a_flota:
        _aviso_caida_a_flota = True
        log(
            f"  AVISO: no existe {configurada}; se usa la clave de la flota\n"
            f"         ({flota}). Si esto sale de un servicio, es que arrancó\n"
            "         antes de que DO_SSH_KEY_FILE existiera; 'remoto <maquina>"
            " update'\n         lo reinicia y deja de hacer falta la caída."
        )
    return flota


def cmd_keys(args: argparse.Namespace) -> None:
    claves = account_keys()
    if getattr(args, "prune", ""):
        return _podar_claves(claves, args.prune, getattr(args, "yes", False))
    for key in claves:
        marca = "  <- flota" if key["name"] == NOMBRE_CLAVE_FLOTA else ""
        log(f"{key['id']:<12} {key['name']:<28} {key['fingerprint']}{marca}")


def _podar_claves(claves: list[dict], patron: str, yes: bool) -> None:
    """Borra de la cuenta las claves cuyo NOMBRE encaje con el patron.

    Existe porque `hacer_lanzador()` registraba un par nuevo EN CADA dev, y los
    dev se destruyen mientras las claves se quedan: medidas 26 `lanzador-dev` el
    2026-09-10 de un total de 31. Con `DO_SSH_KEYS=` vacio (todas), cada droplet
    nuevo nacia con las 31 dentro, 26 de maquinas que ya no existen.

    Nunca toca la clave de la flota ni la de esta maquina, encaje lo que encaje
    el patron: son las dos que dejarian sin acceso a las maquinas nuevas.
    """
    import fnmatch

    protegidos_nombre = {NOMBRE_CLAVE_FLOTA}
    protegidos_material = set()
    for fichero in (Path(cfg("DO_SSH_KEY_FILE")).expanduser(), ruta_clave_flota()):
        pub = ruta_publica(fichero)
        if pub.exists():
            trozos = pub.read_text(encoding="utf-8").strip().split()
            if len(trozos) >= 2:
                protegidos_material.add(trozos[1])

    candidatas = [
        k
        for k in claves
        if k["name"] not in protegidos_nombre
        and k["public_key"].split()[1] not in protegidos_material
        and fnmatch.fnmatch(k["name"], patron)
    ]

    if not candidatas:
        log(f"Ninguna clave encaja con '{patron}' (sin contar las protegidas).")
        return
    log(f"Encajan {len(candidatas)} claves con '{patron}':")
    for key in candidatas:
        log(f"  {key['id']:<12} {key['name']:<28} {key['fingerprint']}")
    log(
        "\nBorrarlas NO echa a nadie de una maquina que ya existe: sus claves ya\n"
        "  estan copiadas en el authorized_keys de cada droplet. Lo unico que\n"
        "  cambia es que dejan de meterse en los droplets NUEVOS."
    )
    if not yes and not confirmar(f"Borrar {len(candidatas)} claves. Escribe 'si': "):
        log("No se borra nada.")
        return
    for key in candidatas:
        api("DELETE", f"/v2/account/keys/{key['id']}")
        log(f"  borrada {key['name']} ({key['id']})")
    log(f"Listo: {len(candidatas)} borradas, {len(claves) - len(candidatas)} quedan.")


def cmd_clave_flota(args: argparse.Namespace) -> None:
    """Genera y registra la clave compartida de la flota. Idempotente.

    Es `keygen` + `register-key` de una sola clave con NOMBRE FIJO, y el valor
    esta en el nombre fijo: cualquier droplet creado despues la lleva en root y
    en el usuario de desarrollo, porque `DO_SSH_KEYS=` vacio significa "todas las
    de la cuenta". A partir de ahi, cualquier maquina de la flota entra en
    cualquier otra sin que importe cual nacio primero.

    La decision incomoda, dicha entera: una clave compartida significa que quien
    entre en una maquina de la flota entra en las demas. Se acepta porque YA era
    asi -esa maquina lleva DO_TOKEN, y con el se puede destruir el mini, hacerle
    una snapshot o crear una maquina nueva con la clave que uno quiera-. La clave
    de flota no sube el techo del dano; solo lo hace utilizable para lo que
    queremos.
    """
    path = ruta_clave_flota()
    pub = ruta_publica(path)
    if path.exists():
        log(f"Ya existe {path}, no se toca.")
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            ["ssh-keygen", "-t", "ed25519", "-f", str(path), "-N", "",
             "-C", NOMBRE_CLAVE_FLOTA],
            check=True,
        )
        log(f"Par de claves de la flota creado en {path}")

    publica = pub.read_text(encoding="utf-8").strip()
    material = publica.split()[1]
    for key in account_keys():
        if key["public_key"].split()[1] == material:
            log(f"Ya estaba registrada en la cuenta como '{key['name']}'.")
            break
    else:
        key = api(
            "POST", "/v2/account/keys",
            {"name": NOMBRE_CLAVE_FLOTA, "public_key": publica},
        )["ssh_key"]
        log(f"Registrada en la cuenta: '{key['name']}' (id {key['id']}).")

    log(
        "\nLos droplets creados A PARTIR DE AHORA la aceptaran solos.\n"
        "  Los que YA existen, no, y por eso hace falta el otro comando:\n"
        "    python scripts/do_droplet.py autorizar-flota <maquina>"
    )


def cmd_autorizar_flota(args: argparse.Namespace) -> None:
    """Autoriza la clave de la flota DENTRO de una maquina que ya existia.

    Es el arranque en frio del problema: el `mini` de hoy nacio antes que la
    clave, asi que no la tiene y nunca la tendria. Esto no rehace la maquina, la
    repara.

    Autoriza para root Y para el usuario de desarrollo, y las dos hacen falta:
    `run_remote_script` entra SIEMPRE como root -hay que escribir en el home de
    otro usuario y hacer chown-, asi que autorizar solo a `deploy` deja fuera
    `push-secret`, `push-github-token`, `push-dir` y `provision`, que son
    justamente los que importan.
    """
    pub = ruta_publica(ruta_clave_flota())
    if not pub.exists():
        die(
            f"No existe {pub}. Crea la clave de la flota primero:\n"
            "  python scripts/do_droplet.py clave-flota"
        )
    publica = pub.read_text(encoding="utf-8").strip()
    droplet, ip, port = resolve_target(args.name or "", args.port or 0)
    usuarios = args.usuario or ["root", cfg("DO_DEV_USER")]
    log(f"Autorizando la clave de la flota en '{droplet['name']}' para: "
        + ", ".join(usuarios))

    lineas = ["set -eu", f"PUB={shq(publica)}",
              'MAT=$(printf "%s" "$PUB" | cut -d" " -f2)']
    for usuario in usuarios:
        lineas += [
            f"U={shq(usuario)}",
            'H=$(getent passwd "$U" | cut -d: -f6)',
            'if [ -z "$H" ]; then echo "  $U: no existe, me lo salto"; else',
            '  install -d -m 700 -o "$U" -g "$U" "$H/.ssh"',
            '  touch "$H/.ssh/authorized_keys"',
            # Se compara el MATERIAL de la clave y no la linea entera: el
            # comentario del final cambia entre maquinas y no distingue una
            # clave de otra.
            '  if grep -q "$MAT" "$H/.ssh/authorized_keys"; then',
            '    echo "  $U: ya estaba autorizada"',
            "  else",
            # El \n va DOBLE: lo tiene que ver printf en el droplet, no Python
            # aqui. Con uno solo, el script remoto llega partido en dos lineas.
            '    printf "%s\\n" "$PUB" >> "$H/.ssh/authorized_keys"',
            '    chown "$U:$U" "$H/.ssh/authorized_keys"',
            '    chmod 600 "$H/.ssh/authorized_keys"',
            '    echo "  $U: autorizada"',
            "  fi",
            "fi",
        ]
    if run_remote_script(ip, port, "\n".join(lineas)) != 0:
        die("Fallo al autorizar. La salida de ssh esta justo arriba.")
    log(f"  ahora cualquier maquina de la flota entra en '{droplet['name']}'")

    # Y la otra mitad, que por defecto tambien se hace: la clave PRIVADA, para
    # que esta maquina pueda SALIR. Un par que solo deja entrar no es un par:
    # el `mini` reparado el 2026-09-10 aceptaba a la flota y no podia tocar a
    # nadie, y `flota` lo canto como "clave flota: NO". Las dos direcciones o
    # ninguna, porque la que falte se descubre el dia que hace falta.
    if args.solo_publica:
        log("\n  --solo-publica: no se manda la privada. Esta maquina deja ENTRAR")
        log("  a la flota pero no puede SALIR hacia las demas.")
        return
    _mandar_clave_flota(droplet["name"], ip, port, cfg("DO_DEV_USER"), ruta_clave_flota())
    log(f"\nListo: '{droplet['name']}' entra y deja entrar. Compruebalo con:")
    log("  python scripts/do_droplet.py flota")


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


def tipo_por_nombre(nombre_droplet: str) -> str:
    """Si hay un tipo que se llama igual que el droplet, ése es el que toca.

    Existe para que el comando quepa en un mensaje de Telegram. Escribir
    `launch bench-control --make-launcher --push-env VAST_AI_API_TOKEN --repo
    …` desde el móvil es exactamente la clase de cosa que se escribe mal, y un
    error de dedo ahí crea una máquina que factura y no sirve.

    No es magia silenciosa: cuando pasa, `launch` lo dice antes de crear nada.
    Y se puede desactivar para un lanzamiento suelto con `--type otro`.
    """
    if not nombre_droplet:
        return ""
    return nombre_droplet if (TYPES_DIR / f"{nombre_droplet}.json").exists() else ""


def resolver_maquina(args: argparse.Namespace) -> dict:
    """Decide plan, imagen, región, arranque y tag combinando las fuentes.

    Manda lo más explícito: una opción de la línea de comandos por encima del
    tipo, y el tipo por encima del .env. Así `--type gpu-h100 --region tor1`
    hace lo que parece, sin tener que editar el descriptor para un lanzamiento
    suelto.

    Si no se pide tipo, se busca uno que se llame como el droplet antes de caer
    en DO_TYPE: es lo que permite que `launch bench-control` traiga consigo sus
    repos, sus variables y su clave de Vast sin escribirlos.
    """
    nombre = args.type or tipo_por_nombre(getattr(args, "name", "") or "") or cfg("DO_TYPE")
    tipo = load_type(nombre) if nombre else {}
    if tipo and not args.type:
        log(f"Tipo '{nombre}' (por el nombre del droplet). Con --type usas otro.")
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


def lista_unida(de_args: list[str], del_tipo) -> list[str]:
    """Une lo de la línea de comandos con lo del tipo, sin repetir y sin perder.

    Se SUMAN en vez de pisarse, al revés que `size` o `region`: un `--repo` suelto
    quiere decir "y además éste", no "olvida los del tipo". Pisarlos haría que
    añadir un repo a mano te dejara la máquina sin los que el tipo daba por
    hechos, y eso no se ve hasta que entras y falta medio trabajo.

    ⚠ Pero sumar siempre dejaba SIN FORMA de decir "ninguno", y eso mordió el
    2026-09-10: `launch mini2 --type mini --service ""` se lanzó justamente para
    NO levantar un segundo bot, y levantó uno igual, porque la cadena vacía se
    descartaba y quedaba la lista del tipo. Resultado: dos Lanzadores con el
    mismo token peleándose por el `getUpdates`, y el mini de verdad reiniciándose
    en bucle hasta que se destruyó el segundo.

    De ahí la cadena vacía EXPLÍCITA como "ninguno". Sólo cuenta si viene de la
    línea de comandos: un tipo con `"services": [""]` sería un descriptor mal
    escrito, no una intención.
    """
    if isinstance(del_tipo, str):
        del_tipo = [del_tipo]
    # `--service ""` (o `--repo ""`) es la forma de decir "ninguno, ni los del
    # tipo". Se mira antes de unir nada, porque después ya no se distingue.
    if any(str(v).strip() == "" for v in (de_args or [])):
        return []
    salida: list[str] = []
    for valor in list(de_args or []) + list(del_tipo or []):
        for parte in str(valor).split(","):
            parte = parte.strip()
            if parte and parte not in salida:
                salida.append(parte)
    return salida


def material_de_la_flota_registrado(keys: list[dict]) -> bool:
    """La clave de la flota está aquí Y está entre las que el droplet llevará.

    Las dos mitades hacen falta: tener el fichero no sirve si nadie registró la
    pública, y estar registrada no sirve si aquí no está la privada.
    """
    pub = ruta_publica(ruta_clave_flota())
    if not ruta_clave_flota().exists() or not pub.exists():
        return False
    trozos = pub.read_text(encoding="utf-8").strip().split()
    if len(trozos) < 2:
        return False
    return any(k["public_key"].split()[1] == trozos[1] for k in keys)


def comprobar_clave_de_entrada(keys: list[dict]) -> None:
    """Que la clave con la que se ENTRARÁ esté entre las que el droplet llevará.

    Es gratis y se hace antes de crear nada, por lo mismo que el token de
    GitHub y el volumen: el fallo de acceso se descubría **después**, con la
    máquina creada, tras ~1 minuto de sondas rechazadas (`RECHAZOS_FATALES`) o
    —antes de que eso existiera— tras los 1.800 s del plazo entero. Y lo que
    quedaba no era un error: era un droplet vivo, facturando, a medio hacer y
    sin nadie que pudiera entrar a rematarlo.

    Comparamos contra `selected_keys()`, que es exactamente la lista que se
    embebe en el droplet, no contra "las de la cuenta" en general: si alguien
    pone `DO_SSH_KEYS` a mano, la pregunta sigue siendo la correcta.

    Y si la clave elegida no entra pero la de la flota **sí está registrada**,
    se cambia a ella en vez de morir. Esto no es un lujo: es el caso real del
    2026-09-11. En el dev, `~/.ssh/do_droplet` **existía** —creado a las 18:22
    con `keygen`, comentario `dev`, y nunca registrado en la cuenta—, así que
    "si no existe, cae a la flota" no le servía de nada: el fichero estaba, y
    era el fichero equivocado. Un fichero que existe y no autentica es tan
    inservible como uno que falta, y aquí sí se puede distinguir, porque
    tenemos delante la lista de claves que el droplet va a llevar.

    Lo que NO hace es bloquear cuando no puede saber. Sin la `.pub` al lado no
    se puede comparar el material, y negarse ahí dejaría sin lanzar a quien
    tenga la privada sola. Se avisa y se sigue: no saber no es saber que va mal,
    ni al revés.
    """
    privada = fichero_clave_ssh()
    if not privada.exists():
        die(
            f"No existe la clave con la que habría que entrar: {privada}\n"
            "  No se ha creado ningún droplet.\n"
            f"  Si esta máquina es de la flota:  python scripts/do_droplet.py clave-flota\n"
            f"  Si no:  python scripts/do_droplet.py keygen && python scripts/do_droplet.py register-key"
        )
    pub = ruta_publica(privada)
    if not pub.exists():
        log(f"  AVISO: falta {pub}, no puedo comprobar si esa clave entrará.")
        return
    trozos = pub.read_text(encoding="utf-8").strip().split()
    if len(trozos) < 2:
        log(f"  AVISO: no entiendo {pub}, no puedo comprobar si esa clave entrará.")
        return
    if any(k["public_key"].split()[1] == trozos[1] for k in keys):
        return
    if material_de_la_flota_registrado(keys) and ruta_clave_flota() != privada:
        # No se muere teniendo delante una clave que SÍ entra. Se cambia por el
        # resto del proceso -`cfg()` lee el entorno, así que `ssh_command()` la
        # usa sin que nadie más se entere- y se dice en voz alta: la variable de
        # esa máquina sigue apuntando mal y eso hay que arreglarlo aparte.
        os.environ["DO_SSH_KEY_FILE"] = str(ruta_clave_flota())
        log(
            f"  AVISO: {privada} no está registrada en la cuenta; se usa la\n"
            f"         clave de la flota ({ruta_clave_flota()}), que sí lo está.\n"
            "         Si esto sale de un servicio, su entorno arrancó sin"
            " DO_SSH_KEY_FILE:\n"
            "         'remoto <maquina> update' lo reinicia y deja de hacer falta."
        )
        return
    die(
        f"La clave con la que se entraría NO está registrada en la cuenta:\n"
        f"  {privada}\n\n"
        "El droplet se crearía con las claves de la cuenta y ésa no es una de\n"
        "ellas, así que nacería inaccesible: existiendo, facturando y sin nadie\n"
        "dentro. No se ha creado ningún droplet.\n\n"
        f"  Las de la cuenta:  python scripts/do_droplet.py keys\n"
        f"  Registrar ésta:    python scripts/do_droplet.py register-key --file {pub}\n"
        f"  O usar la de la flota, que es la buena entre máquinas de la flota:\n"
        f"    python scripts/do_droplet.py clave-flota"
    )


def cmd_launch(args: argparse.Namespace) -> None:
    name = args.name or cfg("DO_DROPLET_NAME")
    if find_droplets(name=name):
        die(f"Ya existe un droplet llamado '{name}'. Usa otro nombre o destrúyelo primero.")

    maquina = resolver_maquina(args)
    # Un tipo no es sólo hardware: es todo lo que hace falta para que esa
    # máquina sirva para lo suyo. Sin esto, la mitad de la definición vivía en
    # un comando largo que hay que recordar y teclear bien cada vez.
    tipo = maquina["tipo"]
    args.repo = lista_unida(args.repo, tipo.get("repos"))
    args.service = lista_unida(args.service, tipo.get("services"))
    args.push_env = lista_unida(args.push_env, tipo.get("push_env"))
    args.make_launcher = args.make_launcher or bool(tipo.get("make_launcher"))
    args.push_do_token = args.push_do_token or bool(tipo.get("push_do_token"))
    # El llavero lo pide el TIPO, no el comando: una máquina de la flota lo lleva
    # siempre, y tener que acordarse de una opción para que lo lleve es la forma
    # de que un día no lo lleve. `--llavero` suelto sirve para una máquina que no
    # tiene tipo.
    args.llavero = args.llavero or tipo_pide_llavero(tipo)
    size = None if args.no_check else comprobar_size(
        maquina["size"], maquina["region"], args.accept_cost
    )

    keys = selected_keys()

    # El token de GitHub, antes de crear nada y por lo mismo que el volumen: una
    # máquina que nace sin sus repos privados factura igual que una buena y no
    # lo dice. Con --dry-run no hace falta, que no crea nada.
    if not args.dry_run and not args.no_provision:
        # Primero ésta, que es local e instantánea: si no vamos a poder entrar,
        # no hace falta ni preguntarle a GitHub.
        comprobar_clave_de_entrada(keys)
        comprobar_github_token(args.sin_github)
        # Y el llavero, aqui y no dentro de provision: si a esta maquina le
        # faltan secretos obligatorios, el droplet no se llega a crear. Fallar
        # gratis es lo barato; fallar con la maquina ya facturando, no.
        if args.llavero:
            comprobar_llavero(args.sin_llavero)

    # El volumen se comprueba antes de crear el droplet: si la región no cuadra
    # o el nombre está mal escrito, el fallo tiene que salir gratis y no
    # dejarte una máquina facturando sin el disco que ibas a usar.
    vol_name = args.volume or tipo.get("volume") or cfg("DO_VOLUME")
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

    provision_ok = True
    if port and not args.no_provision:
        log("")
        provision_ok = cmd_provision(
            argparse.Namespace(
                name=name,
                port=port,
                repo=args.repo,
                service=args.service,
                push_do_token=args.push_do_token,
                push_env=args.push_env,
                make_launcher=args.make_launcher,
                sin_github=args.sin_github,
                llavero=args.llavero,
                sin_llavero=args.sin_llavero,
                skip_wait=False,
                desde_launch=True,
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

    if tipo.get("post") and port and not args.no_provision:
        ejecutar_post(name, ip, port, tipo["post"])

    key_file = fichero_clave_ssh()
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
        hubo_url = False
        for svc in selected_services(args.service):
            log(f"\n  Servicio '{svc['name']}' corriendo. Estado y logs:")
            log(f"  python scripts/do_droplet.py service logs {svc['name']}")
            # Sin `port` no hubo SSH, así que no hay a quién preguntarle.
            if not svc["url"] or not port:
                continue
            direccion, aviso = url_de_servicio(
                svc, name, ip, port, cfg("DO_DEV_USER")
            )
            if direccion:
                log(f"  Ábrelo:  {direccion}")
                hubo_url = True
            else:
                log(f"  AVISO: {aviso}")
        if hubo_url:
            # Va aquí y no en el descriptor porque vale para cualquier servicio
            # que se publique: si la dirección lleva el token dentro, ES la
            # llave, y se acaba de imprimir en una terminal (o en un chat).
            log("\n  (si esa dirección lleva token, ES la llave: quien la tenga, entra)")
    log(f"\n  Al terminar:    python scripts/do_droplet.py destroy {name}")
    log("  (el droplet factura por segundo mientras exista)")
    log("=" * 62)

    if not provision_ok:
        # Aquí al final y no en cuanto se supo: la máquina ya existe y factura,
        # así que el resumen de arriba -IP, cómo entrar, cómo destruirla- tiene
        # que salir entero antes de morir. Y se muere: un lanzamiento que sale
        # con 0 se lee como "está lista", y no lo está. Desde el bot, además, el
        # código != 0 es lo único que hace llegar este texto al chat.
        die(
            f"'{name}' EXISTE Y FACTURA, pero nació a medias: le falta algún repo\n"
            "  (los nombres están arriba, en la salida del aprovisionamiento).\n"
            f"  Arregla el token y repite:  python scripts/do_droplet.py provision {name}\n"
            f"  O destrúyela:               python scripts/do_droplet.py destroy {name}"
        )


def ejecutar_post(name: str, ip: str, port: int, comandos) -> None:
    """Comandos del tipo que se ejecutan DENTRO del droplet al final del todo.

    Es lo que remata una máquina que tiene que poder usar otra nube: el token de
    Vast.ai la deja ALQUILAR, pero no ENTRAR. Como en DigitalOcean, una instancia
    acepta las claves registradas en el momento de crearla, así que sin un
    `vast_instance.py register-key` previo se alquilan máquinas a las que su
    creador no puede conectarse: existen, facturan y no sirven. Con `post` en el
    descriptor, eso deja de ser un paso que hay que acordarse de dar.

    Corren como el usuario de desarrollo y con shell de login, para que vean los
    tokens de `dev-secrets.env`; con root o sin login, `register-key` fallaría
    con un "falta el token" en una máquina donde el token sí está.

    Ninguno es fatal: para cuando corren, la máquina ya está creada y
    aprovisionada, y tumbarlo todo por un paso final sería peor que avisar.
    """
    if isinstance(comandos, str):
        comandos = [comandos]
    dev_user = cfg("DO_DEV_USER")
    log(f"\nPasos finales del tipo ({len(comandos)}):")
    for comando in comandos:
        log(f"  $ {comando}")
        script = "\n".join(
            [
                "set -eu",
                f"DEV_USER={shq(dev_user)}",
                f'sudo -u "$DEV_USER" -H bash -lc {shq(comando)}',
            ]
        )
        if run_remote_script(ip, port, script) != 0:
            log(
                f"  AVISO: falló. La máquina existe y está aprovisionada.\n"
                f"  Reintenta desde dentro:  python scripts/do_droplet.py ssh {name} "
                f"--cmd {shq(comando)}"
            )


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


def pre_destroy_script() -> str:
    """El script que se corre DENTRO del droplet antes de destruirlo.

    Es genérico: recorre los descriptores y ejecuta lo que cada servicio declare
    en `pre_destroy`. Este fichero no sabe qué recoge ninguno de ellos -si lo
    supiera, el enrutado se habría comido el dominio (R18)-; sabe que hay cosas
    que sólo la propia máquina puede recoger, y que hay que dejarla intentarlo
    antes de tirar del cable.

    Ya hay precedente exacto en este mismo fichero: `cmd_volume` entra por SSH a
    hacer `umount` antes de desconectar el volumen.

    El repo se busca en el disco en vez de suponer el usuario: quién corre el
    servicio es un hecho de la máquina, y se lee de ahí (R16).
    """
    partes = ["set -u"]
    for svc in all_services():
        if not svc["pre_destroy"]:
            continue
        nombre, carpeta = svc["name"], svc["dir"]
        partes += [
            f'D=$(ls -d /home/*/src/{carpeta} /root/src/{carpeta} 2>/dev/null | head -1 || true)',
            'if [ -n "${D:-}" ] && [ -d "$D" ]; then',
            f'  echo "  {nombre}: recogiendo antes de destruir…"',
            '  U=$(stat -c %U "$D")',
            f'  (cd "$D" && sudo -u "$U" -H bash -lc {shq(svc["pre_destroy"])})'
            f' || echo "  AVISO: {nombre}: la recogida falló; se destruye igual."',
            "else",
            f'  echo "  {nombre}: no está instalado aquí; nada que recoger."',
            "fi",
        ]
    return "\n".join(partes)


def limpiar_antes_de_destruir(droplet: dict, timeout: int = 30) -> None:
    """Deja que la máquina recoja lo suyo antes de destruirla. NUNCA levanta.

    ⚠⚠ ESTO NO PUEDE IMPEDIR QUE SE DESTRUYA EL DROPLET, y por eso se traga todo
    y lleva un timeout corto. Si la recogida pudiera tumbar el apagado, una
    molestia -que el nombre del nodo siga ocupado un rato- se convertiría en una
    factura -un droplet vivo que nadie apaga-. Es la misma lección del
    2026-09-04: si el aviso puede matar el trabajo, ya no es una comodidad.

    Y se DICE qué pasó en las tres ramas -recogido, no se pudo (con el motivo),
    o nadie declara nada-, porque un silencio aquí es indistinguible de que
    funcionara.
    """
    # ⚠⚠ EL `try` EMPIEZA EN LA PRIMERA LÍNEA, y no es estilo: `pre_destroy_script()`
    # llama a `all_services()` -> `load_service()`, que hace `die()` -o sea
    # `SystemExit`- si CUALQUIER descriptor de services/ está roto. Con la
    # construcción fuera del try, un JSON malo de un servicio que ni siquiera
    # está en este droplet abortaba `cmd_destroy` entera ANTES de borrar nada:
    # un fichero mal editado dejaba droplets vivos que nadie apaga. Es justo la
    # molestia-convertida-en-factura que esta función existe para no causar.
    # (Encontrado al revisar este mismo commit, 2026-09-10. Tiene test.)
    #
    # `KeyboardInterrupt` NO se traga: si tú cortas, quieres cortarlo todo.
    nombre = droplet.get("name", "?")
    try:
        script = pre_destroy_script()
        if script.strip() == "set -u":
            return  # ningún servicio declara `pre_destroy`: no hay nada que hacer

        ip = public_ip(droplet)
        if not ip:
            log(f"  {nombre}: sin IP, no puedo recoger nada. Se destruye igual.")
            return
        port = wait_for_ssh(ip, timeout=timeout)
        if not port:
            log(f"  {nombre}: no contesta por SSH en {timeout}s. Se destruye igual.")
            return
        run_remote_script(ip, port, script, timeout=timeout * 2)
    except (Exception, SystemExit) as exc:  # noqa: BLE001 - ver arriba
        log(f"  {nombre}: no pude recoger ({type(exc).__name__}: {exc}). Se destruye igual.")


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
        limpiar_antes_de_destruir(d)
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
    key_file = str(fichero_clave_ssh())
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


def run_remote_script(ip: str, port: int, script: str, timeout: float | None = None) -> int:
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
        timeout=timeout,
    )
    return proc.returncode


def run_remote_split(ip: str, port: int, script: str) -> tuple[int, str, str]:
    """Como run_remote_capture, pero con stdout y stderr SEPARADOS.

    Existe porque hay salidas que se leen a máquina y no a ojo: cuando lo que
    vuelve es un DATO (una dirección), mezclarlo con el saludo de un `.bashrc` o
    con un aviso de ssh lo corrompe. Quien sólo quiere enseñárselo al usuario
    sigue usando `run_remote_capture`, que los junta como siempre.
    """
    proc = subprocess.run(
        ssh_command(ip, port, user="root") + ["bash -s"],
        input=script.encode("utf-8"),
        capture_output=True,
    )
    return (
        proc.returncode,
        proc.stdout.decode("utf-8", errors="replace"),
        proc.stderr.decode("utf-8", errors="replace"),
    )


def run_remote_capture(ip: str, port: int, script: str) -> tuple[int, str]:
    """Como run_remote_script, pero devolviendo también lo que imprimió.

    Hace falta para el camino de ida y vuelta de `--make-launcher`: la clave
    privada se genera DENTRO del droplet y no sale de ahí nunca; lo que vuelve
    es la pública, que no es secreta, para registrarla en la cuenta.
    """
    code, salida, err = run_remote_split(ip, port, script)
    return code, salida + err


# Repo del propio lanzador. Una máquina que va a lanzar droplets lo necesita
# clonado: es el programa que sabe hablar con la API.
REPO_LANZADOR = "stalinbeltran/digital-ocean-dropplet-auto-launching"


def _mandar_clave_flota(
    name: str, ip: str, port: int, dev_user: str, privada: Path
) -> None:
    """Copia la clave de la flota al droplet y la deja como su clave de trabajo.

    Escribe TRES cosas, y las tres hacen falta:
      - la privada en `~/.ssh/do_flota`, modo 600 del usuario de desarrollo;
      - la publica al lado, que es lo que `register-key` mira desde dentro;
      - `DO_FLEET_KEY_FILE` y `DO_SSH_KEY_FILE` en dev-secrets.env, para que
        `ssh_command()` de esa maquina la use sin que nadie se lo diga.

    Sin la tercera, la maquina tendria la clave y seguiria intentando entrar con
    otra: el fichero existe, el acceso no funciona, y no hay ningun error que lo
    explique.
    """
    texto_privada = privada.read_text(encoding="utf-8")
    texto_publica = ruta_publica(privada).read_text(encoding="utf-8").strip()
    destino = FICHERO_CLAVE_FLOTA.replace("~/", "")

    script = "\n".join(
        [
            "set -eu",
            "umask 077",
            f"DEV_USER={shq(dev_user)}",
            'H=$(getent passwd "$DEV_USER" | cut -d: -f6)',
            '[ -n "$H" ] || { echo "no existe el usuario $DEV_USER" >&2; exit 1; }',
            f'KEY="$H/{destino}"',
            'install -d -m 700 -o "$DEV_USER" -g "$DEV_USER" "$H/.ssh"',
            # Heredoc con el delimitador entrecomillado: una clave privada lleva
            # `$` y backslashes, y sin las comillas el shell los expandiria.
            'cat > "$KEY" <<\'FIN_CLAVE\'',
            texto_privada.rstrip("\n"),
            "FIN_CLAVE",
            'cat > "$KEY.pub" <<\'FIN_PUB\'',
            texto_publica,
            "FIN_PUB",
            'chmod 600 "$KEY"',
            'chmod 644 "$KEY.pub"',
            'chown "$DEV_USER:$DEV_USER" "$KEY" "$KEY.pub"',
            # Y que la use: si no, la clave esta puesta y la maquina sigue
            # intentando entrar con otra.
            'F="$H/.config/dev-secrets.env"',
            'install -d -m 700 -o "$DEV_USER" -g "$DEV_USER" "$H/.config"',
            'touch "$F"',
            'grep -v "^export DO_FLEET_KEY_FILE=" "$F" > "$F.tmp" || true',
            'grep -v "^export DO_SSH_KEY_FILE=" "$F.tmp" > "$F.tmp2" || true',
            'mv "$F.tmp2" "$F"; rm -f "$F.tmp"',
            f'echo "export DO_FLEET_KEY_FILE=$H/{destino}" >> "$F"',
            f'echo "export DO_SSH_KEY_FILE=$H/{destino}" >> "$F"',
            'chmod 600 "$F"',
            'chown "$DEV_USER:$DEV_USER" "$F"',
            'echo "  clave de la flota puesta en $KEY"',
        ]
    )
    if run_remote_script(ip, port, script) != 0:
        log(
            "  AVISO: no pude poner la clave de la flota. La maquina puede CREAR\n"
            "         droplets pero quiza no entrar en ellos."
        )
        return
    log(f"  '{name}' usa la clave de la flota: entra en cualquier maquina de la")
    log("  flota, y cualquiera entra en ella. No se registra ninguna clave nueva.")


def hacer_lanzador(name: str, ip: str, port: int) -> None:
    """Deja al droplet en condiciones de crear y usar otros droplets.

    El token por sí solo no basta, y esa es la parte que se olvida: con él la
    máquina puede CREAR droplets, pero no ENTRAR en ellos. Un droplet acepta las
    claves públicas que estén registradas en la cuenta en el momento de crearlo,
    así que la máquina lanzadora necesita un par propio y su pública registrada
    ANTES de lanzar nada. Sin esto se crean máquinas a las que su creador no
    puede conectarse: existen, facturan y no sirven.

    Hay DOS caminos, y el bueno es el primero:

    1. **Si esta maquina tiene la clave de la flota, se manda esa.** Una sola
       clave, registrada una vez en la cuenta, que llevan todas las maquinas.
       Es lo que rompe la dependencia del ORDEN DE NACIMIENTO: sin ella, la
       clave de un dev se registra durante SU provision -siempre despues de que
       el mini exista- y por eso el dev nunca ha podido entrar en el mini.
       Ademas se acaba el goteo: 26 claves `lanzador-dev` muertas en la cuenta
       el 2026-09-10, una por cada dev que existio alguna vez.
    2. **Si no la hay, como siempre**: se genera un par EN el destino y vuelve
       la publica para registrarla. Se conserva para no dejar sin arreglo una
       maquina lanzada con `--sin-llavero` o desde un sitio sin clave de flota.

    En el camino 1 la privada SI viaja, por SSH y por stdin como los tokens, a
    un fichero 600. En el 2 no viaja nunca: se genera en el destino y solo
    vuelve la publica, que no es secreta.
    """
    dev_user = cfg("DO_DEV_USER")
    flota = ruta_clave_flota()
    if flota.exists() and ruta_publica(flota).exists():
        return _mandar_clave_flota(name, ip, port, dev_user, flota)
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


def diagnostico_de_arranque(ip: str, port: int, timeout: int = 45) -> str:
    """Qué está haciendo la máquina AHORA, preguntándoselo a ella.

    Existe para meterlo DENTRO del mensaje de error, no para imprimirlo por el
    camino: cuando el lanzamiento sale del bot de Telegram, el coordinador sólo
    publica `stderr` y sólo si el código no es 0, así que todo lo que se cuente
    por `stdout` mientras se espera no llega a ningún sitio. Un diagnóstico que
    no llega al chat es un diagnóstico que no existe para quien lanza del móvil.

    Se traga cualquier fallo y devuelve texto igualmente: esto se llama cuando
    algo ya ha ido mal, y no puede tapar el fallo que estaba contándose.
    """
    guion = r"""
echo "- cloud-init: $(cloud-init status 2>&1 | head -1)"
# `unattended-upgrade-shutdown` esta SIEMPRE ahi esperando una senal, asi que
# nombrarlo seria ruido constante. Lo que importa es un apt de verdad: si sale
# uno aqui, el `package_upgrade` de cloud-init esta peleando por el cerrojo.
echo "- apt en marcha: $(pgrep -a -f 'apt-get|unattended-upgrade' \
  | grep -v unattended-upgrade-shutdown | head -2 | tr '\n' ';')"
if [ -e /var/log/dev-tools-install.log ]; then
  echo "- ultimas lineas de /var/log/dev-tools-install.log:"
  tail -5 /var/log/dev-tools-install.log | sed "s/^/    /"
else
  echo "- /var/log/dev-tools-install.log todavia no existe:"
  echo "  cloud-init no ha llegado siquiera a instalar las herramientas."
fi
"""
    try:
        salida = subprocess.run(
            ssh_command(ip, port, user="root") + ["bash -s"],
            input=guion.encode("utf-8"),
            capture_output=True,
            timeout=timeout,
        )
    except Exception as exc:  # noqa: BLE001 - ver el docstring
        return f"  (no se pudo preguntar a la máquina: {type(exc).__name__})"
    texto = salida.stdout.decode("utf-8", "replace").strip()
    if not texto:
        pega = salida.stderr.decode("utf-8", "replace").strip().splitlines()
        return "  (la máquina no contestó" + (f": {pega[-1]}" if pega else "") + ")"
    return "\n".join("  " + linea for linea in texto.splitlines())


def wait_for_dev_tools(ip: str, port: int, timeout: int = 0) -> None:
    """Espera a que cloud-init termine de instalar Node, Claude Code y gh.

    SSH responde bastante antes de que cloud-init acabe, así que inyectar los
    secretos nada más conectar pillaría la máquina a medio hacer.

    ⚠ Aquí EL SILENCIO ES EL FALLO, y costó una mañana entera de investigación.
    El 2026-09-10 por la noche dos `launch dev` seguidos desde el mini murieron
    con "se agotó la espera" y nada más: ni si la máquina iba lenta, ni si
    estaba atascada, ni si la sonda llegaba siquiera. Y no llegar se parece
    demasiado a no estar lista -una sonda que falla por SSH deja `stdout`
    vacío, exactamente igual que un "todavía no"-, así que los dos casos se
    confundían en el mismo bucle mudo de quince minutos.

    De ahí las tres cosas que hace ahora, y ninguna es cosmética:
      - mira el `stderr` de la sonda, para distinguir "no está lista" de "no
        llego a ella";
      - cuenta cada pocos minutos qué está haciendo la máquina, para quien
        mira la terminal;
      - y mete el diagnóstico DENTRO del error, para quien lanzó desde el móvil
        y sólo va a ver eso.
    """
    timeout = timeout or int(cfg("DO_DEV_TOOLS_TIMEOUT") or 1800)
    inicio = time.time()
    deadline = inicio + timeout
    warned = False
    proximo_parte = inicio + 180
    ultima_pega = ""
    rechazos = 0
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
            # Un arranque MUY lento tiene que dejar rastro aunque acabe bien.
            # Subir el plazo de 900 a 1800 s arregló que se perdieran
            # lanzamientos, pero de paso convirtió el arranque patológico en un
            # éxito mudo: antes fallaba y al menos se notaba. Esto se imprime
            # por `stdout`, que en un `launch` que sale con 0 es lo que el
            # coordinador SÍ publica en el chat.
            tardanza = int(time.time() - inicio)
            if tardanza > LENTO_SOSPECHOSO:
                log(
                    f"  OJO: la instalación tardó {tardanza} s. Lo medido el "
                    f"2026-09-11 fueron 272 y 348 s desde el arranque, así que"
                )
                log("       esto no es normal. Si se repite, mira el arranque:")
                log(diagnostico_de_arranque(ip, port))
            return
        if state == "FAILED":
            die(
                "La instalación de herramientas falló en el droplet. Mira el log:\n"
                "  python scripts/do_droplet.py ssh --cmd "
                "'tail -40 /var/log/dev-tools-install.log'"
            )
        if state != "WAIT":
            # La sonda no llegó a correr: lo que ha fallado es el SSH, no la
            # instalación. Sin esto, un ssh que no conecta deja `stdout` vacío y
            # es indistinguible de un "todavía no", así que el bucle esperaba en
            # silencio hasta agotar el plazo y el error no decía por qué. Eso no
            # se puede depurar desde un chat de Telegram.
            lineas = (probe.stderr or "").strip().splitlines()
            pega = lineas[-1] if lineas else f"ssh salió con {probe.returncode}"
            if pega != ultima_pega:
                log(f"  la comprobación no llega a la máquina: {pega}")
                ultima_pega = pega
            # ⚠ Y hay un motivo que NO es cuestión de esperar más: si la máquina
            # rechaza la clave, va a rechazarla las mil veces siguientes. Esperar
            # media hora a que eso cambie es perder media hora y, desde el móvil,
            # no enterarse de nada. Se cuentan seguidos -no el primero, que puede
            # caer mientras sshd aún se está colocando- y se muere diciendo con
            # QUÉ clave se estaba intentando, que es el dato que falta.
            if "Permission denied" in pega or "Too many authentication" in pega:
                rechazos += 1
                if rechazos >= RECHAZOS_FATALES:
                    die(
                        f"El droplet RECHAZA la clave. No es que no esté listo:\n"
                        f"  {pega}\n\n"
                        f"Se está intentando con:  {fichero_clave_ssh()}\n"
                        f"  DO_SSH_KEY_FILE dice:  {cfg('DO_SSH_KEY_FILE')}"
                        f"{'' if Path(cfg('DO_SSH_KEY_FILE')).expanduser().exists() else '  (NO EXISTE)'}\n"
                        f"  clave de la flota:     {ruta_clave_flota()}"
                        f"  ({'existe' if ruta_clave_flota().exists() else 'tampoco existe'})\n"
                        "Si la de la flota existe y aun así se rechaza, no es cuestión\n"
                        "de qué clave: esa está registrada en la cuenta y el droplet\n"
                        "nace con TODAS las de la cuenta dentro.\n\n"
                        "Mira si esa clave está registrada en la cuenta:\n"
                        "  python scripts/do_droplet.py keys\n"
                        "Un droplet sólo acepta las claves registradas CUANDO SE CREÓ.\n\n"
                        "OJO: el droplet SIGUE VIVO Y FACTURANDO.\n"
                        "  python scripts/do_droplet.py destroy <nombre> --yes"
                    )
            else:
                rechazos = 0
        if not warned:
            # La espera larga no son las herramientas -40 s medidos el
            # 2026-09-11, con Node, Claude Code, gh y uv dentro-, sino apt y
            # los scripts de DigitalOcean, que corren antes.
            log("  esperando a que cloud-init termine de instalar (unos 3 min)…")
            warned = True
        if time.time() >= proximo_parte:
            log("  sigue sin terminar. Esto es lo que dice la máquina:")
            log(diagnostico_de_arranque(ip, port))
            proximo_parte = time.time() + 300
        time.sleep(10)
    die(
        f"Se agotó la espera ({timeout} s) a que el droplet terminase de instalar\n"
        "las herramientas. OJO: EL DROPLET SIGUE VIVO Y FACTURANDO.\n\n"
        "Lo que dice la máquina ahora mismo:\n"
        + diagnostico_de_arranque(ip, port)
        + (f"\n  (la comprobación se quejaba de: {ultima_pega})" if ultima_pega else "")
        + "\n\nY OJO CON LO QUE ESA MAQUINA ES AHORA MISMO: `launch` muere aquí,\n"
        "que es ANTES de aprovisionar. O sea que no tiene secretos, ni repos, ni\n"
        "servicios: sistema operativo y SSH, y nada más. Si esperabas que su bot\n"
        "de Telegram contestase, no va a contestar, y no es que esté rota.\n\n"
        "Si la instalación sigue avanzando, dale tiempo y remátala con:\n"
        "  python scripts/do_droplet.py provision <nombre>\n"
        "Si está atascada, destrúyela para no pagarla:\n"
        "  python scripts/do_droplet.py destroy <nombre> --yes"
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
    # El SIMÉTRICO de `install`: qué tiene que recoger la máquina antes de que la
    # destruyan. El lanzador no sabe qué recoge —eso es del servicio—, sólo sabe
    # correrlo por SSH y seguir pase lo que pase. Ver `limpiar_antes_de_destruir`.
    svc.setdefault("pre_destroy", "")
    # Cómo se PRESENTA el servicio: un comando que imprime la dirección con la
    # que se abre. Opcional; sin él, `launch` no anuncia ninguna. El detalle de
    # por qué es un comando y no un patrón de URL, en `url_de_servicio`.
    svc.setdefault("url", "")
    svc.setdefault("env_prefix", "")
    svc.setdefault("env_file", ".env")
    # Ficheros que el servicio necesita y que no están en su repo. Sin esto hay
    # configuración que sólo vive dentro del droplet y se pierde al destruirlo,
    # que es justo lo contrario de poder tirar y rehacer una máquina.
    svc.setdefault("files", {})
    if not isinstance(svc["files"], dict):
        die(f"{path}: 'files' tiene que ser un objeto de ruta -> contenido.")
    return svc


def all_services() -> list[dict]:
    """Todos los descriptores de services/, sin filtrar por lo que esté activo.

    `selected_services` responde a "qué va a correr en esta máquina";  esto, a
    "qué hay definido en el repo", que es lo que necesita el catálogo de ayuda:
    desde el móvil se consulta el catálogo justamente cuando no te acuerdas de
    lo que hay.
    """
    if not SERVICES_DIR.exists():
        return []
    return [load_service(p.stem) for p in sorted(SERVICES_DIR.glob("*.json"))]


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


def url_de_servicio(
    svc: dict, name: str, ip: str, port: int, dev_user: str
) -> tuple[str, str]:
    """La dirección con la que se abre el servicio, preguntándosela a ÉL.

    Devuelve `(direccion, aviso)`, y exactamente uno de los dos trae texto.

    El lanzador no sabe de puertos, de tokens ni de formatos de dirección: sabe
    pedirle a cada servicio que se presente. El descriptor declara el comando
    (`url`) y el repo del servicio lo implementa, que es el mismo reparto que
    `install` y `start` — la lógica vive en quien produce, no en quien
    transporta. Cablear aquí `http://<ip>:8010/?t=…` habría metido en el
    lanzador el puerto y el formato del token de un proyecto concreto, y habría
    que tocarlo cada vez que cambie cualquiera de los dos.

    ⚠ Corre como el usuario de desarrollo y NO como root, y de eso depende que
    funcione: el token de la puerta suele vivir en el `~/.config` de ESE usuario,
    así que con root `~` apunta a `/root` y el comando contestaría "no hay token"
    en una máquina donde sí lo hay.

    ⚠ El contrato es la ÚLTIMA línea no vacía de stdout, no toda la salida: el
    comando corre con shell de login (que es como ve `dev-secrets.env`), y un
    `.bashrc` que salude ensuciaría la dirección. Por eso hace falta
    `run_remote_split`: con stderr mezclado, un aviso de ssh valdría por
    respuesta.

    ⚠ Y si esa línea no trae esquema (`://`) se trata como fallo. Imprimir algo
    con pinta de dirección que no lo es manda al usuario a una página que no
    carga con todo lo de arriba diciendo "listo", y un dato que parece bueno y no
    lo es cuesta más que no darlo.

    ⚠ La dirección puede llevar una CREDENCIAL dentro (la web app de
    foveal-vision manda su token como `?t=`, que es la única forma de pasárselo a
    un móvil). Vuelve por el canal cifrado de ssh y por stdin, nunca en la línea
    de comandos —que se ve en el `ps` del droplet—, pero al imprimirla queda en
    la terminal y, si se lanzó desde Telegram, en el chat.

    Nunca es fatal: para cuando esto corre la máquina ya está creada y
    aprovisionada, y quedarse sin saber la dirección es una molestia, mientras
    que tumbar el lanzamiento por ello sería peor (criterio de `ejecutar_post`).
    """
    dentro = f'cd "$HOME/src/{svc["dir"]}" && {svc["url"]}'
    script = "\n".join(
        [
            "set -eu",
            f"DEV_USER={shq(dev_user)}",
            f'sudo -u "$DEV_USER" -H bash -lc {shq(dentro)}',
        ]
    )
    code, salida, err = run_remote_split(ip, port, script)
    lineas = [l.strip() for l in salida.splitlines() if l.strip()]
    if code == 0 and lineas and "://" in lineas[-1]:
        return lineas[-1], ""
    pistas = [l.strip() for l in (salida + "\n" + err).splitlines() if l.strip()]
    return "", (
        f"{svc['name']}: no pude preguntarle su dirección "
        f"({pistas[-1] if pistas else 'no imprimió nada'}).\n"
        f"  Pregúntasela desde dentro:  python scripts/do_droplet.py ssh {name} "
        f"--cmd {shq(dentro)}"
    )


LLAVERO_PATH = ROOT / "llavero.json"


def cargar_llavero() -> list[dict]:
    """Lee llavero.json: lo que lleva CUALQUIER máquina de la flota.

    Dato, no código, como `types/` y `services/`. Existe porque hasta el
    2026-09-10 lo que una máquina llevaba era la unión de tres cosas que no se
    miraban juntas en ningún sitio -lo que `build_provision_script` escribe a
    pelo, el `push_env` del tipo y el barrido por prefijo de `env_prefix`- y la
    tercera no declara nombres: barre el entorno. Si no encuentra nada, no falla:
    suelta un AVISO entre cien líneas y sigue.

    Lo que eso producía, medido ese día en el `mini`: llevaba `TG_*` y no
    `TGL_*`, o sea que sabía parir un dev y no sabía parir un mini. Y de sus 19
    variables, `types/mini.json` declaraba UNA.
    """
    if not LLAVERO_PATH.exists():
        die(
            f"Falta {LLAVERO_PATH}, que es donde se declara qué secretos lleva\n"
            "  una máquina de la flota. Sin él no se puede comprobar nada."
        )
    try:
        datos = json.loads(LLAVERO_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        die(f"{LLAVERO_PATH} no es JSON válido: {exc}")
    variables = datos.get("variables")
    if not isinstance(variables, list) or not variables:
        die(f"{LLAVERO_PATH}: falta la lista 'variables' o está vacía.")
    for var in variables:
        if not isinstance(var, dict) or not var.get("nombre"):
            die(f"{LLAVERO_PATH}: cada variable necesita al menos 'nombre'.")
        var.setdefault("obligatoria", False)
        var.setdefault("porque", "")
    return variables


def tipo_pide_llavero(tipo: dict) -> bool:
    """Si este tipo de máquina lleva el llavero entero.

    Explícito y no deducido de `make_launcher`: un secreto no viaja a donde no
    hace falta (objetivo 5), y hay máquinas que lanzan cosas sin ser de la flota.
    """
    return bool(tipo.get("llavero"))


def comprobar_llavero(sin_llavero: bool = False) -> list[tuple[str, str]]:
    """Pares (nombre, valor) del llavero presentes aquí, o muerte si falta uno obligatorio.

    Se llama ANTES de crear el droplet y ANTES de tocar una máquina ya creada,
    por lo mismo que `comprobar_github_token()`: una máquina que nace sin la
    mitad de su llavero factura igual que una buena y no lo dice. El síntoma
    llega días después y no se parece a la causa.

    Muere en vez de avisar a propósito. El precedente está medido: hasta el
    2026-09-06 un repo sin clonar era un `AVISO` en mitad de la salida con
    `exit 0`, así que el lanzamiento daba por bueno el trabajo con la máquina ya
    rota.
    """
    variables = cargar_llavero()
    presentes: list[tuple[str, str]] = []
    faltan: list[dict] = []
    for var in variables:
        valor = os.environ.get(var["nombre"], "").strip()
        if valor:
            presentes.append((var["nombre"], valor))
        elif var["obligatoria"]:
            faltan.append(var)

    if faltan and sin_llavero:
        log(
            f"  --sin-llavero: faltan {len(faltan)} secretos obligatorios y se sigue "
            "igual.\n  La máquina nacerá coja: "
            + ", ".join(v["nombre"] for v in faltan)
        )
        return presentes
    if faltan:
        detalle = "\n".join(f"    {v['nombre']:<26} {v['porque']}" for v in faltan)
        die(
            f"A esta máquina le faltan {len(faltan)} secretos del llavero, y sin ellos\n"
            "  la que se cree nacería coja. No se ha creado ni tocado nada.\n\n"
            f"{detalle}\n\n"
            "  Arréglalo poniéndolos en el .env de ESTA máquina. De dónde sale cada\n"
            "  uno está en el manual del repo central:\n"
            "    estudios-redes-neuronales/docs/secretos-desde-cero.md\n"
            "  Si es otra máquina la que los tiene, tráetelos:\n"
            "    python scripts/do_droplet.py llavero traer <maquina>\n"
            "  Y si de verdad quieres una máquina coja, repítelo con --sin-llavero."
        )
    log(f"  Llavero: {len(presentes)}/{len(variables)} variables listas para viajar.")
    return presentes


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


# Código con el que sale el script de aprovisionamiento cuando se hizo TODO
# menos clonar algún repo. Tiene número propio porque no es lo mismo que "no se
# aprovisionó": la máquina quedó utilizable, pero a medias, y eso hay que
# decirlo alto en vez de dejarlo en un AVISO entre cien líneas de salida.
PROVISION_INCOMPLETO = 3

# Lo que contesta GitHub sobre el token, cacheado: lo preguntan `launch` (antes
# de crear nada) y `provision` (antes de tocar la máquina), y la respuesta es la
# misma.
_GITHUB_ESTADO: tuple[str, str] | None = None


def github_token_estado(tok: str) -> tuple[str, str]:
    """Le pregunta a GitHub si el token SIRVE, no si está. Devuelve el veredicto.

    ('ok', login) | ('rechazado', detalle) | ('duda', detalle).

    Un token caducado o revocado tiene exactamente el mismo aspecto que uno
    bueno: mismo prefijo `github_pat_`, misma longitud, y se copia igual de bien
    a sus tres destinos. Lo único que los distingue es preguntar. Medido el
    2026-09-06: el `mini` llevaba días transportando a los droplets que creaba
    un token que GitHub rechazaba con 401, y no lo dijo ni el envío ni el
    aprovisionamiento.

    'duda' no es 'ok': un 403 por rate limit o una red caída significan que no
    lo sé, y no saber ni puede bloquear un lanzamiento ni puede pasar por bueno.
    """
    global _GITHUB_ESTADO
    if _GITHUB_ESTADO is not None:
        return _GITHUB_ESTADO
    req = urllib.request.Request(
        "https://api.github.com/user",
        headers={
            "Authorization": f"Bearer {tok}",
            "Accept": "application/vnd.github+json",
            # GitHub exige User-Agent: sin él contesta 403 y parecería otra cosa.
            "User-Agent": "do_droplet.py",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            login = json.loads(resp.read().decode("utf-8")).get("login", "?")
        _GITHUB_ESTADO = ("ok", login)
    except urllib.error.HTTPError as e:
        if e.code == 401:
            _GITHUB_ESTADO = ("rechazado", "HTTP 401: Bad credentials")
        else:
            _GITHUB_ESTADO = ("duda", f"GitHub contestó HTTP {e.code}")
    except (urllib.error.URLError, OSError) as e:
        _GITHUB_ESTADO = ("duda", f"no pude preguntar a GitHub: {e}")
    return _GITHUB_ESTADO


def comprobar_github_token(sin_github: bool = False) -> str:
    """Token de GitHub que se va a enviar, o muerte si el que hay no sirve.

    Se llama ANTES de crear el droplet y ANTES de tocar una máquina ya creada,
    por lo mismo que se comprueba el volumen: un token muerto no da ningún
    síntoma inmediato -los repos privados no se clonan y ya está-, así que la
    máquina nace sin la mitad de su trabajo y el fallo aparece días después
    como un `git push` que no autentica.

    Y reescribir `dev-secrets.env` con un token muerto además BORRA el que
    hubiera en el destino, que podía ser bueno; de ahí que bloquee también en
    `provision` y no sólo en `launch`.
    """
    if sin_github:
        log("  --sin-github: no se envía token de GitHub. Los repos privados no se clonarán.")
        return ""
    tok = os.environ.get("GITHUB_TOKEN", "").strip()
    if not tok:
        log(
            "  AVISO: no hay GITHUB_TOKEN. No podrás clonar repos privados.\n"
            "  Créalo en https://github.com/settings/personal-access-tokens"
        )
        return ""
    estado, detalle = github_token_estado(tok)
    if estado == "ok":
        log(f"  GitHub: el token sirve (usuario {detalle}).")
        return tok
    if estado == "duda":
        log(f"  AVISO: no he podido comprobar el GITHUB_TOKEN ({detalle}). Sigo igual.")
        return tok
    die(
        f"El GITHUB_TOKEN de esta máquina no sirve: {detalle}.\n"
        "  No se ha creado ni tocado nada; parar aquí es lo barato. Con ese token\n"
        "  la máquina nacería sin sus repos privados y sin poder empujar nada.\n"
        "  OJO con la pista falsa: `credential.helper store` borra la credencial\n"
        "  en cuanto GitHub la rechaza una vez, así que ~/.git-credentials queda\n"
        "  en 0 bytes y eso se lee como 'nunca llegó el token', que es justo lo\n"
        "  contrario de lo que pasó.\n"
        "  Arréglalo:\n"
        "    1. token nuevo en https://github.com/settings/personal-access-tokens\n"
        "       (Contents: read and write)\n"
        "    2. GITHUB_TOKEN=... en el .env de ESTA máquina\n"
        "    3. a cada máquina viva:  python scripts/do_droplet.py push-github-token <nombre>\n"
        "       (escribe los TRES destinos: dev-secrets.env, .git-credentials y gh)\n"
        "  Si de verdad quieres una máquina sin GitHub, repítelo con --sin-github."
    )


def build_provision_script(
    repos: list[str],
    services: list[dict] | None = None,
    push_do_token: bool = False,
    push_env: list[str] | None = None,
    github_token: str = "",
    llavero: list[tuple[str, str]] | None = None,
) -> str:
    """Script que deja el droplet listo para trabajar.

    Todo lo secreto se escribe con umask 077 y acaba en modo 600 del usuario de
    desarrollo. Nada de esto puede ir en cloud-init: el user_data lo sirve la API
    de metadatos y lo lee cualquier proceso del droplet sin privilegios.

    El token de GitHub llega como argumento y no se lee del entorno aquí: quien
    llama ya lo ha comprobado con `comprobar_github_token()`, y volver a leerlo
    sería la forma de saltarse esa comprobación sin enterarse.
    """
    dev_user = cfg("DO_DEV_USER")
    claude_token = os.environ.get("CLAUDE_CODE_OAUTH_TOKEN", "").strip()

    # Un solo diccionario en vez de ir apilando líneas: el llavero, el token de
    # GitHub y `push_env` se solapan (GITHUB_TOKEN está en los tres caminos), y
    # con líneas sueltas el fichero salía con `export X=` repetido. No rompe
    # -al sourcear gana la última- pero hace ilegible el destino y esconde
    # justo lo que uno va a mirar cuando algo no autentica.
    valores: dict[str, str] = {}

    # El llavero primero: es la base sobre la que lo explícito manda.
    for nombre, valor in llavero or []:
        valores[nombre] = valor

    if claude_token:
        valores["CLAUDE_CODE_OAUTH_TOKEN"] = claude_token
    if github_token:
        # GH_TOKEN lo lee gh; GITHUB_TOKEN lo esperan casi todas las herramientas.
        valores["GITHUB_TOKEN"] = github_token
        valores["GH_TOKEN"] = github_token

    exports = ["# Generado por do_droplet.py provision. Modo 600, no lo copies."]
    if push_do_token:
        # Sólo para la máquina de control, y sólo pidiéndolo a mano: con este
        # token el droplet puede crear y destruir máquinas en la cuenta, o sea
        # gastar dinero. Va aquí y no sólo en el .env del bot para que también
        # lo tengan las sesiones de la máquina; si no, `register-key` desde
        # dentro falla con un "falta el token" que despista.
        valores["DO_TOKEN"] = token()

    # Config del lanzador que se lleva la máquina de control, para que los
    # droplets que cree ella salgan iguales que los que creas tú: mismos repos,
    # misma autoría de git, mismos servicios. Sin esto lanza máquinas peores y
    # no se nota hasta que entras en una.
    for nombre in push_env_names(push_env or []):
        valor = os.environ.get(nombre, "").strip()
        if not valor:
            log(f"  AVISO: --push-env {nombre} no tiene valor aquí, no se envía.")
            continue
        valores[nombre] = valor

    # Ordenadas por nombre: el destino se lee con los ojos cuando algo no
    # autentica, y un orden que depende de en qué rama se añadió cada una
    # convierte un `diff` entre dos máquinas en ruido.
    exports += [f"export {n}={shq(v)}" for n, v in sorted(valores.items())]

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
            # Los que no se puedan clonar se apuntan aquí y se cobran al final.
            "FALTAN=''",
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
                f'|| FALTAN="$FALTAN {slug}"',
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

    if repos:
        # Un repo que no se clona no da NINGÚN síntoma después: no hay error, no
        # hay servicio caído, sólo falta un directorio que nadie mira hasta que
        # se necesita. Hasta el 2026-09-06 esto era un AVISO en mitad de la
        # salida y el script seguía saliendo con 0, así que el lanzamiento daba
        # por bueno el trabajo con la máquina ya rota. Se cobra al final -lo
        # hecho, hecho está- y con código propio. Por stderr, porque es lo
        # único que llega al chat cuando quien lanza el comando es el bot.
        parts += [
            "",
            'if [ -n "$FALTAN" ]; then',
            '  echo "" >&2',
            '  echo "La máquina quedó A MEDIAS: no pude clonar:$FALTAN" >&2',
            '  echo "  Lo demás -credenciales, servicios- sí está puesto." >&2',
            '  echo "  Suele ser el GITHUB_TOKEN de la máquina lanzadora'
            ' (caducado o revocado), o un repo que dejó de ser público." >&2',
            f"  exit {PROVISION_INCOMPLETO}",
            "fi",
        ]

    return "\n".join(parts) + "\n"


def reiniciar_servicios(ip: str, port: int, services: list[dict]) -> None:
    """Reinicia los servicios instalados para que vean el entorno FINAL.

    ⚠⚠ ESTO NO ES HIGIENE, es la diferencia entre una máquina que funciona y una
    que miente. **El entorno de un proceso es una FOTO de cuando arrancó**, y
    aquí los servicios arrancan A MITAD del aprovisionamiento: `hacer_lanzador()`
    y los `post` del tipo escriben en `dev-secrets.env` DESPUÉS. Todo lo que se
    escriba a partir de ese punto es invisible para el servicio **para siempre**,
    y ningún error lo delata.

    Medido el 2026-09-11, y costó dos días y media docena de droplets: el mini
    recién hecho arrancaba su bot antes de que existiera
    `DO_SSH_KEY_FILE=~/.ssh/do_flota`, así que todo `launch` que salía del bot
    caía al defecto `~/.ssh/do_droplet` -una clave local que NADIE registró en la
    cuenta-, y se quedaba sondeando con `Permission denied` hasta agotar el
    plazo. Desde una sesión SSH el mismo comando funcionaba, porque un shell de
    login sí lee el fichero. El mismo comando, dos entornos, y sólo uno roto:
    por eso no se reproducía.

    Un fallo aquí no aborta nada: para cuando esto corre, la máquina ya está
    hecha, y tumbar el lanzamiento por un `systemctl` sale peor que avisar.

    ⚠⚠ ...Y ESO ERA FALSO HASTA EL 2026-09-11 POR LA TARDE, en la misma función
    que lo afirma. `services` son los **dicts** de `selected_services()`, no
    nombres, y esto hacía `shq(s)` sobre el dict: `AttributeError`. La lista por
    comprensión está **fuera** del `try`, así que no avisaba: abortaba el
    `launch` con traceback, justo entre `hacer_lanzador()` y `ejecutar_post()`.

    Medido ese día en el dev nacido a las 15:20 UTC -aprovisionado por un mini
    que YA tenía este arreglo-: la clave de flota se escribió (15:26:06), ningún
    servicio se reinició, y **los dos `post` del tipo no corrieron**. Eso último
    es lo que lo prueba y lo que más costó: el `.env` no tiene la cabecera de
    `entornos aplicar`, y la clave del dev **no quedó registrada en Vast**, o sea
    la máquina podía alquilar instancias en las que no podía entrar.

    La lección, que es la del proyecto sobre el código de salida aplicada a un
    arreglo: se comprueba el **artefacto** (¿se reinició el unit? ¿corrieron los
    `post`?), no que la función exista. Y la anotación decía `list[str]` desde el
    primer día: la pista estaba escrita y no la leyó nadie, porque nada la
    ejecutaba. Por eso ahora hay test.
    """
    if not services:
        return
    log("\nReiniciando los servicios para que vean el entorno final…")
    guion = "\n".join(
        [
            "set -u",
            *[
                f'systemctl restart {shq(u)} 2>/dev/null'
                f' && echo "  {u}: reiniciado"'
                f' || echo "  AVISO: {u}: no se pudo reiniciar; puede que le falten'
                f' variables escritas despues de arrancar."'
                # `services` son los DICTS de selected_services(), no nombres: el
                # unit es svc["name"] (el mismo que usa instalar_servicio). Pasar
                # el dict a shq() reventaba con AttributeError -y FUERA del try-,
                # abortando el launch justo despues de escribir la clave. Ver el
                # aviso del final de este docstring.
                for u in (svc["name"] for svc in services)
            ],
        ]
    )
    try:
        run_remote_script(ip, port, guion, timeout=120)
    except Exception as exc:  # noqa: BLE001 - ver el docstring
        log(f"  AVISO: no se pudieron reiniciar ({type(exc).__name__}). La máquina está hecha.")


def cmd_provision(args: argparse.Namespace) -> bool:
    """Deja la máquina lista. Devuelve False si quedó a medias (algún repo sin clonar).

    Devuelve en vez de morir porque a `launch` le queda trabajo por delante -el
    volumen, los pasos finales del tipo, y sobre todo el resumen con la IP y la
    orden de destruir la máquina que acaba de crear-. Llamado a pelo sí muere,
    que es lo que espera quien lo escribe en una terminal o en el bot.
    """
    name = args.name or cfg("DO_DROPLET_NAME")

    # La configuración se valida antes de ir a buscar el droplet: un servicio mal
    # escrito debe fallar al instante y no tras esperar a que arranque la máquina.
    repos = args.repo or [r for r in cfg("DO_REPOS").split(",") if r.strip()]
    services = selected_services(getattr(args, "service", []) or [])

    # Y el token de GitHub, antes de tocar la máquina: esto reescribe
    # dev-secrets.env ENTERO, así que aprovisionar con un token muerto borra del
    # destino el que hubiera, que podía ser bueno.
    github_token = comprobar_github_token(getattr(args, "sin_github", False))

    # El llavero, por lo mismo y en el mismo sitio. Sólo si quien llama lo pidió:
    # `provision` a pelo sobre una máquina que no es de la flota no tiene por qué
    # exigir los secretos de una que sí lo es.
    llavero = (
        comprobar_llavero(getattr(args, "sin_llavero", False))
        if getattr(args, "llavero", False)
        else []
    )

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
            github_token,
            llavero,
        ),
    )
    # Faltar repos no es lo mismo que no haber aprovisionado: la máquina sirve,
    # pero le falta trabajo. Se sigue hasta el final y se cobra al salir.
    incompleto = code == PROVISION_INCOMPLETO
    if code != 0 and not incompleto:
        die(f"El aprovisionamiento falló (código {code}).")

    if make_launcher:
        log("\nDejando la máquina en condiciones de lanzar droplets…")
        hacer_lanzador(name, ip, port)

    reiniciar_servicios(ip, port, services)

    if incompleto:
        if getattr(args, "desde_launch", False):
            return False
        die(
            f"'{name}' quedó A MEDIAS: le falta algún repo (los nombres, arriba).\n"
            "  Lo demás -credenciales, servicios- sí está puesto.\n"
            "  Arregla el token y repite este mismo comando; clonar lo que falta\n"
            "  no toca lo que ya está."
        )

    log("Aprovisionamiento terminado.")
    return True


# ----------------------------------------------------- actualizar desde dentro


# Marca que build_service_section deja en la Description de cada unidad. Con
# ella se reconocen luego los servicios que instaló este script, sin tener que
# guardar una lista aparte que se desincronizaría.
PROVISION_MARK = "instalado por do_droplet.py provision"


def dentro_del_droplet(comando: str = "update") -> Path:
    """Comprueba que corremos en el droplet y devuelve el ~/src sobre el que actuar.

    `update` e `install-executors` son los comandos que actúan sobre la máquina
    donde se ejecutan en vez de sobre la API: se lanzan dentro del droplet, por
    SSH o desde el bot. Ejecutarlos por error en la laptop haría `git pull` en
    repos que estás tocando a mano y reiniciaría servicios; de ahí la
    comprobación, y de ahí que el mensaje nombre al comando de verdad: uno que
    nombre a otro manda a la persona a copiar la orden equivocada.
    """
    if sys.platform != "linux" or not Path("/var/lib/cloud").exists():
        die(
            f"`{comando}` se ejecuta DENTRO del droplet, no en la máquina "
            "lanzadora.\n"
            "  Desde aquí:  python scripts/do_droplet.py ssh mini --cmd \\\n"
            "    'cd ~/src/digital-ocean-dropplet-auto-launching && "
            f"python3 scripts/do_droplet.py {comando}'"
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


def cmd_push_github_token(args: argparse.Namespace) -> None:
    """Rota el token de GitHub de un droplet ya creado, en sus TRES sitios.

    Existe porque el token de GitHub no vive en un sitio, vive en tres, y
    `push-secret` solo alcanza al primero:

      1. `~/.config/dev-secrets.env` -GITHUB_TOKEN y GH_TOKEN-, que es lo que
         ven las sesiones SSH y los ejecutores del bot;
      2. `~/.git-credentials`, de donde saca git el token al hacer `pull` y
         `push` por https;
      3. la sesion de `gh`, si la maquina lo lleva instalado.

    Dejar el 2 sin tocar es la trampa: el entorno tiene el token nuevo, todo
    parece correcto, y `git pull` sigue mandando el viejo. Con un token revocado
    eso es un `update` que falla por autenticacion en una maquina donde el token
    nuevo si esta, el mismo sintoma despistado de siempre.

    La alternativa -`provision`- reescribiria `dev-secrets.env` entero y borraria
    del destino lo que esta maquina no tenga a mano (el DO_TOKEN del mini, el de
    Vast). Esto toca solo las lineas del token de GitHub y deja el resto igual.

    El token viaja por SSH dentro del script que va por **stdin**, nunca como
    argumento: lo de la linea de comandos sale en el `ps` del destino.

    Repetir el comando ROTA el token: quita las lineas anteriores y pone las
    nuevas.
    """
    valor = os.environ.get(args.from_env, "").strip()
    if not valor:
        die(
            f"La variable '{args.from_env}' no tiene valor en esta maquina.\n"
            "  Es la que se iba a enviar como GITHUB_TOKEN al destino.\n"
            "  Ponla en .env (que esta gitignoreado) antes de enviarla."
        )

    dev_user = cfg("DO_DEV_USER")
    droplet, ip, port = resolve_target(args.name or "", args.port or 0)
    log(f"Enviando el token de GitHub a '{droplet['name']}' ({ip}:{port}).")

    script = "\n".join(
        [
            "set -eu",
            "umask 077",
            f"DEV_USER={shq(dev_user)}",
            'H=$(getent passwd "$DEV_USER" | cut -d: -f6)',
            '[ -n "$H" ] || { echo "no existe el usuario $DEV_USER" >&2; exit 1; }',
            'install -d -m 700 -o "$DEV_USER" -g "$DEV_USER" "$H/.config"',
            # El token se lee a una variable desde un heredoc con el delimitador
            # entrecomillado: no se expande ni se interpreta nada de lo que
            # traiga, y aparece una sola vez en el script pese a ir a tres sitios.
            "GHT=$(cat <<'FIN_TOKEN'",
            valor,
            "FIN_TOKEN",
            ")",
            "",
            "# --- 1) los secretos de la maquina (sesiones SSH y ejecutores del bot)",
            'F="$H/.config/dev-secrets.env"',
            'T="$F.nuevo"',
            "# Se copia el fichero SIN las lineas del token y se le anaden las",
            "# nuevas: sirve igual para ponerlo la primera vez que para rotarlo,",
            "# y ningun otro secreto del destino se toca.",
            'if [ -f "$F" ]; then',
            '  grep -v -e "^export GITHUB_TOKEN=" -e "^export GH_TOKEN=" "$F"'
            ' > "$T" || true',
            "else",
            '  : > "$T"',
            "fi",
            # Aqui el valor va entrecomillado por shq y no por "$GHT": lo que se
            # escribe es una linea que luego SOURCEA un shell, asi que tiene que
            # sobrevivir a que la lea bash, no solo a que la escriba printf.
            # GH_TOKEN lo lee gh; GITHUB_TOKEN lo esperan casi todas las demas.
            "cat >> \"$T\" <<'FIN_VARS'",
            f"export GITHUB_TOKEN={shq(valor)}",
            f"export GH_TOKEN={shq(valor)}",
            "FIN_VARS",
            'mv "$T" "$F"',
            'chmod 600 "$F"',
            'chown "$DEV_USER:$DEV_USER" "$F"',
            'echo "  dev-secrets.env: GITHUB_TOKEN y GH_TOKEN puestos"',
            "",
            *bloque_cargar_secretos(),
            "",
            "# --- 2) las credenciales de git, que es lo que usa `git pull`",
            'sudo -u "$DEV_USER" -H git config --global credential.helper store',
            'printf "https://x-access-token:%s@github.com\\n" "$GHT"'
            ' > "$H/.git-credentials"',
            'chmod 600 "$H/.git-credentials"',
            'chown "$DEV_USER:$DEV_USER" "$H/.git-credentials"',
            'echo "  .git-credentials: reescrito"',
            "",
            "# --- 3) la sesion de gh, si la hay",
            "# Que un fallo aqui no de el comando por perdido: lo importante -git",
            "# y el entorno- ya esta puesto, y la maquina de control no lleva gh.",
            "if ! command -v gh >/dev/null; then",
            '  echo "  gh: no instalado en esta maquina, git si tiene el token"',
            'elif printf "%s\\n" "$GHT" | '
            'sudo -u "$DEV_USER" -H gh auth login --with-token 2>/dev/null; then',
            '  echo "  gh: $(sudo -u "$DEV_USER" -H gh api user --jq .login '
            '2>/dev/null || echo "?")"',
            "else",
            '  echo "  gh: no acepto el token (git si lo tiene)"',
            "fi",
        ]
    )

    if run_remote_script(ip, port, script) != 0:
        die("Fallo el envio del token. La salida de ssh esta justo arriba.")
    log("\nListo. Para comprobar que git autentica, sin sacar el token a pantalla:")
    log(f"  python scripts/do_droplet.py ssh {droplet['name']} --cmd \\")
    log("    'cd ~/src/digital-ocean-dropplet-auto-launching && git fetch && echo OK'")


def cmd_install_executors(args: argparse.Namespace) -> None:
    """DENTRO de una máquina: reescribe los ficheros que declara un servicio.

    Existía por un hueco del ciclo que **ya no está**: `update` hacía `git pull`
    y reiniciaba, o sea traía el CÓDIGO, pero los ficheros del bloque `files` de
    un descriptor los escribía `provision`, desde la laptop.

    Desde 2026-08-22 los ejecutores de este repo viven en `telegram/executors/` y
    el coordinador los DESCUBRE ahí (`data/fuentes.json` trae `~/src/*/telegram`),
    así que llegan con `git pull` y no hay nada que aplicar: basta `actualizar`.
    Esto se queda para descriptores que aún declaren `files` —y para decirlo
    cuando no—, porque un comando que no hace nada en silencio es peor que uno
    que falta.
    """
    base = dentro_del_droplet("install-executors")
    servicios = selected_services(args.service)
    if not servicios:
        die(
            "Dime qué servicio aplicar: --service telegram-launcher\n"
            "  (o pon DO_SERVICES en el .env de esta máquina)"
        )

    escritos, reiniciar = 0, []
    for svc in servicios:
        destino = base / svc["dir"]
        if not destino.is_dir():
            log(f"  {svc['name']}: no existe {destino}, me lo salto.")
            continue
        if not svc["files"]:
            log(
                f"  {svc['name']}: no declara ningún fichero, y es lo normal desde el "
                "2026-08-22.\n"
                "      Sus ejecutores viven en <repo>/telegram/executors/*.json y el "
                "coordinador\n"
                "      los descubre solo. Llegan con `git pull`: basta el ejecutor "
                "`actualizar`."
            )
            continue
        dueno = owner_of(destino)
        # Por servicio y no global: con el contador compartido, escribir algo en
        # el servicio A reiniciaba también el B, que no había cambiado.
        cambiados = 0
        for ruta, contenido in svc["files"].items():
            if ruta.startswith("/") or ".." in ruta:
                die(f"{svc['name']}: la ruta '{ruta}' tiene que ser relativa al repo.")
            texto = (
                contenido
                if isinstance(contenido, str)
                else json.dumps(contenido, indent=2, ensure_ascii=False) + "\n"
            )
            path = destino / ruta
            path.parent.mkdir(parents=True, exist_ok=True)
            antes = path.read_text(encoding="utf-8") if path.exists() else None
            if antes == texto:
                log(f"  {svc['name']}: {ruta} ya estaba al día")
                continue
            path.write_text(texto, encoding="utf-8")
            # Este comando se ejecuta de dos maneras: como el usuario de
            # desarrollo (desde el bot, que es el caso normal) y como root
            # (entrando por SSH a mano). En el primer caso el fichero ya nace
            # con el dueño correcto y `chown` sobra; en el segundo hace falta,
            # porque si no el bot no puede leer sus propios ejecutores. Sólo
            # root puede cambiar el dueño, así que se intenta únicamente ahí.
            if os.geteuid() == 0 and dueno and dueno != "root":
                run_local(["chown", f"{dueno}:{dueno}", str(path)])
            log(f"  {svc['name']}: escrito {ruta}")
            cambiados += 1
            escritos += 1
        if cambiados and svc["name"] not in reiniciar:
            reiniciar.append(svc["name"])

    if not escritos:
        log("Nada que cambiar.")
        return
    for unit in reiniciar:
        log(f"  {reiniciar_unidad(unit, propia=unit == unidad_propia())}")


def cmd_install_service(args: argparse.Namespace) -> None:
    """DENTRO de una máquina: instala un servicio de `services/` sin rehacerla.

    El hueco que tapa: `update` trae el código y reinicia lo que YA está
    instalado, pero una unidad NUEVA sólo la escribía `provision`, desde la
    máquina lanzadora. Así que declarar un servicio en `types/dev.json` llegaba a
    los droplets futuros y no al que lo declaró, y la única salida era rehacer
    una máquina que estaba perfectamente viva. Pasó al añadir
    `foveal-vision-web` (2026-08-29).

    ⚠ Se reusa `build_service_section`, el MISMO generador que usa `provision`.
    Un segundo escritor de la misma unidad diverge del primero, y el que se
    depura luego es siempre el que no escribiste tú.

    La instalación puede tardar (un `npm ci` o un venv de torch son minutos), así
    que la salida va en directo en vez de capturarse: desde el móvil, un comando
    mudo diez minutos no se distingue de uno colgado.
    """
    base = dentro_del_droplet("install-service")
    servicios = selected_services(args.service)
    if not servicios:
        die(
            "Dime qué servicio instalar: --service foveal-vision-web\n"
            "  (o pon DO_SERVICES en el .env de esta máquina)"
        )

    # El dueño de ~/src es quien tiene que ser dueño del servicio: deducirlo del
    # disco es correcto aquí porque la pregunta es literalmente "de quién son
    # estos repos", no "dónde vive la configuración".
    dev_user = owner_of(base) or cfg("DO_DEV_USER")
    if not dev_user or dev_user == "root":
        die(f"No sé de qué usuario son los repos de {base}; pon DO_DEV_USER en el .env.")

    faltan = [s["name"] for s in servicios if not (base / s["dir"]).is_dir()]
    if faltan:
        die(
            f"No están clonados los repos de: {', '.join(faltan)}.\n"
            f"  Clónalos en {base} y repite; instalar una unidad sobre un "
            "directorio que no existe sólo produce un servicio que no arranca."
        )

    lineas = [
        "set -eu",
        "umask 077",
        f"DEV_USER={shq(dev_user)}",
        'H=$(getent passwd "$DEV_USER" | cut -d: -f6)',
        '[ -n "$H" ] || { echo "no existe el usuario $DEV_USER" >&2; exit 1; }',
    ]
    for svc in servicios:
        lineas += build_service_section(svc, dev_user)
    guion = "\n".join(lineas) + "\n"

    log(f"Instalando en {socket.gethostname()}: "
        f"{', '.join(s['name'] for s in servicios)}")
    cmd = ["bash", "-s"] if os.geteuid() == 0 else ["sudo", "-n", "bash", "-s"]
    try:
        proc = subprocess.run(cmd, input=guion, text=True, timeout=args.timeout)
    except subprocess.TimeoutExpired:
        die(f"Se agotó el tiempo ({args.timeout}s). El servicio puede haber quedado a medias:\n"
            f"  systemctl status {servicios[0]['name']}")
    except OSError as exc:
        die(str(exc))
    if proc.returncode != 0:
        die(f"La instalación falló (código {proc.returncode}).")
    log("Listo. Estado y logs:")
    for svc in servicios:
        log(f"  systemctl status {svc['name']}  ·  journalctl -u {svc['name']} -n 30")


def cmd_push_service_env(args: argparse.Namespace) -> None:
    """Añade o rota variables en el `.env` de un servicio, sin borrar el resto.

    Hermano de `push-do-token`, y por el mismo motivo: `provision` reescribe ese
    fichero ENTERO, así que usarlo para añadir una variable borra del destino lo
    que el emisor no tenga a mano. Aquí se sustituye línea a línea.

    El puente de nombres es el de siempre: `TGL_VAST_AI_API_TOKEN` en el .env de
    esta máquina llega como `VAST_AI_API_TOKEN` al del servicio. Así el secreto
    vive en un único sitio -esta laptop- y sólo alcanza a la máquina a la que se
    lo mandas, en vez de colarse en todos los droplets.
    """
    svc = load_service(args.service)
    prefijo = svc["env_prefix"]
    if not prefijo:
        die(f"El servicio '{svc['name']}' no declara env_prefix: no hay puente de nombres.")

    nombres = push_env_names(args.vars)
    if not nombres:
        die("Dime qué variables enviar, p. ej.: VAST_AI_API_TOKEN")

    pares = []
    for nombre in nombres:
        # Se acepta escribir el nombre de destino (VAST_AI_API_TOKEN) o el de
        # origen (TGL_VAST_AI_API_TOKEN): desde el móvil se recuerda mal cuál es.
        destino = nombre[len(prefijo):] if nombre.startswith(prefijo) else nombre
        valor = os.environ.get(prefijo + destino, "").strip()
        if not valor:
            die(
                f"'{prefijo}{destino}' no tiene valor en esta máquina.\n"
                f"  Ponlo en .env como {prefijo}{destino}=… (el .env está gitignoreado)."
            )
        pares.append((destino, valor))

    droplet, ip, port = resolve_target(args.name or "", args.port or 0)
    dev_user = cfg("DO_DEV_USER")
    log(
        f"Enviando {', '.join(n for n, _ in pares)} al .env de "
        f"'{svc['name']}' en {droplet['name']} ({ip}:{port})."
    )

    lineas = [
        "set -eu",
        "umask 077",
        f"DEV_USER={shq(dev_user)}",
        'H=$(getent passwd "$DEV_USER" | cut -d: -f6)',
        f'F="$H/src/{svc["dir"]}/{svc["env_file"]}"',
        '[ -f "$F" ] || { echo "no existe $F; aprovisiona el servicio antes" >&2; exit 1; }',
        'T="$F.nuevo"',
        'cp "$F" "$T"',
    ]
    for destino, valor in pares:
        lineas += [
            f'grep -v "^{destino}=" "$T" > "$T.tmp" || true',
            f'mv "$T.tmp" "$T"',
            f"cat >> \"$T\" <<'FIN_VAR'",
            f"{destino}={valor}",
            "FIN_VAR",
        ]
    lineas += [
        'mv "$T" "$F"',
        'chmod 600 "$F"',
        'chown "$DEV_USER:$DEV_USER" "$F"',
        'echo "  $F: $(grep -c . "$F") variables"',
        f"systemctl restart {svc['name']}.service || true",
        f'echo "  {svc["name"]}: $(systemctl is-active {svc["name"]}.service)"',
    ]

    if run_remote_script(ip, port, "\n".join(lineas)) != 0:
        die("Falló el envío. La salida de ssh está justo arriba.")
    log("Listo. El servicio se reinició para recogerlas.")


def cmd_executors(args: argparse.Namespace) -> None:
    """Imprime el catálogo de ejecutores del bot, con ejemplos.

    Lee la convención que usa el coordinador desde 2026-08-22: cada repo declara
    los suyos en `<repo>/telegram/executors/*.json`, y la descripción va en el
    MISMO fichero que el ejecutor (campos `descripcion` y `ejemplos`), así que no
    hay dos sitios que puedan divergir. Antes vivían en el bloque `files` de un
    descriptor de `services/` y la descripción en un bloque `ayuda` paralelo.

    Esto es la versión de la laptop; desde Telegram lo mismo lo da `/executors`,
    que ya los describe. Aquí se listan TODOS los repos, no sólo los de este
    servicio: se consulta para saber qué hay, no qué corre en esta máquina.
    """
    raices = [Path(d).expanduser() for d in (args.dir or ["~/src"])]
    hubo = False
    for raiz in raices:
        if not raiz.is_dir():
            log(f"(no existe {raiz}, me lo salto)")
            continue
        for carpeta in sorted(raiz.glob("*/telegram/executors")):
            fichas = sorted(carpeta.glob("*.json"))
            if not fichas:
                continue
            hubo = True
            log(f"Ejecutores de '{carpeta.parent.parent.name}':\n")
            for f in fichas:
                try:
                    d = json.loads(f.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError) as e:
                    log(f"  {f.name}: NO SE PUDO LEER ({e})\n")
                    continue
                log(f"  {d.get('name', f.stem)}")
                log(f"      {d.get('descripcion') or '(sin describir en su JSON)'}")
                for ej in d.get("ejemplos") or []:
                    log(f"      > {d.get('name', f.stem)}  {ej}" if ej else f"      > {d.get('name', f.stem)}")
                log("")
    if not hubo:
        log(
            "Ningún repo declara ejecutores.\n"
            "  Se definen en <repo>/telegram/executors/*.json y el coordinador los\n"
            "  descubre solo (su data/fuentes.json trae ~/src/*/telegram)."
        )


def cmd_push_secret(args: argparse.Namespace) -> None:
    """Escribe variables en `~/.config/dev-secrets.env`, sin borrar las demás.

    Hace falta porque el `.env` de un servicio y los secretos de la MÁQUINA son
    dos sitios distintos, y la diferencia se descubre tarde. El coordinador pasa
    su entorno a cada ejecutor (`runner.ts`: `{...process.env}`), así que una
    variable del `.env` del bot alcanza al bot y a nadie más: entrando por SSH a
    esa misma máquina no existe. Ya nos pasó con `DO_TOKEN` y un `register-key`
    que fallaba con un "falta el token" incomprensible en una máquina donde el
    token sí estaba.

    Lo que se escribe aquí lo cargan las tres formas de usar la máquina -sesión
    interactiva, shell de login y `ssh maquina 'comando'`- porque la línea que
    lo lee va al principio de `.bashrc`.

    Repetir el comando ROTA el valor: quita la línea anterior y pone la nueva.
    """
    # El llavero entero de una vez: es el camino de REPARACIÓN de una máquina
    # viva. `provision` también lo escribe, pero reescribe dev-secrets.env con
    # `cat >`, así que usarlo sólo para añadir borra del destino lo que el emisor
    # no tenga a mano. Esto toca únicamente las líneas que nombra.
    if getattr(args, "llavero", False):
        pares = comprobar_llavero(getattr(args, "sin_llavero", False))
        if not pares:
            die("El llavero de esta máquina está vacío: no hay nada que enviar.")
        return _escribir_secretos(args, pares)

    nombres = push_env_names(args.vars)
    if not nombres:
        die(
            "Dime qué variables enviar, p. ej.: VAST_AI_API_TOKEN\n"
            "  O manda el llavero entero:  push-secret --llavero --name <maquina>"
        )

    pares = []
    for nombre in nombres:
        valor = os.environ.get(nombre, "").strip()
        if not valor and args.prefix:
            valor = os.environ.get(args.prefix + nombre, "").strip()
        if not valor:
            alt = f" ni '{args.prefix}{nombre}'" if args.prefix else ""
            die(
                f"'{nombre}'{alt} no tiene valor en esta máquina.\n"
                "  Ponlo en .env (que está gitignoreado) antes de enviarlo."
            )
        pares.append((nombre, valor))

    return _escribir_secretos(args, pares)


def _escribir_secretos(args: argparse.Namespace, pares: list[tuple[str, str]]) -> None:
    """Escribe pares en dev-secrets.env del destino, MEZCLANDO y sin borrar nada.

    Vive aparte porque lo usan los dos caminos -unas pocas variables por nombre y
    el llavero entero- y no pueden divergir: si uno de los dos se dejara la linea
    que carga el fichero en .bashrc, el secreto existiria en la maquina y no lo
    leeria nadie, que es el fallo que mas cuesta reconocer.
    """
    dev_user = cfg("DO_DEV_USER")
    droplet, ip, port = resolve_target(args.name or "", args.port or 0)
    log(
        f"Enviando {', '.join(n for n, _ in pares)} a los secretos de "
        f"'{droplet['name']}' ({ip}:{port})."
    )

    lineas = [
        "set -eu",
        "umask 077",
        f"DEV_USER={shq(dev_user)}",
        'H=$(getent passwd "$DEV_USER" | cut -d: -f6)',
        '[ -n "$H" ] || { echo "no existe el usuario $DEV_USER" >&2; exit 1; }',
        'install -d -m 700 -o "$DEV_USER" -g "$DEV_USER" "$H/.config"',
        'F="$H/.config/dev-secrets.env"',
        'T="$F.nuevo"',
        'if [ -f "$F" ]; then cp "$F" "$T"; else : > "$T"; fi',
    ]
    for nombre, valor in pares:
        lineas += [
            f'grep -v "^export {nombre}=" "$T" > "$T.tmp" || true',
            'mv "$T.tmp" "$T"',
            # Heredoc con el delimitador entrecomillado: nada de lo que haya en
            # el valor se expande ni se interpreta, venga como venga.
            "cat >> \"$T\" <<'FIN_VAR'",
            f"export {nombre}={shq(valor)}",
            "FIN_VAR",
        ]
    lineas += [
        'mv "$T" "$F"',
        'chmod 600 "$F"',
        'chown "$DEV_USER:$DEV_USER" "$F"',
        "",
        *bloque_cargar_secretos(),
        "",
        'echo "  $F: $(grep -c ^export "$F") variables"',
    ]

    if run_remote_script(ip, port, "\n".join(lineas)) != 0:
        die("Falló el envío. La salida de ssh está justo arriba.")
    log("\nListo. Para comprobarlo sin sacar el valor a pantalla:")
    log(f"  python scripts/do_droplet.py ssh {droplet['name']} --cmd \\")
    log("    'cd ~/src/digital-ocean-dropplet-auto-launching && "
        "python3 scripts/vast_instance.py list'")


EXCLUIDOS_POR_DEFECTO = (
    ".git",
    ".venv",
    "venv",
    "node_modules",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
)


def cmd_push_dir(args: argparse.Namespace) -> None:
    """Copia un directorio de esta máquina al droplet, sin pasar por git.

    Existe porque hay cosas que un `git clone` no trae y sin las cuales la
    máquina no puede trabajar: en este repo, el dataset del benchmark
    (`data/sources/dirty-1000-80px`, unos 30 MB) está gitignoreado, así que el
    droplet de control clona foveal-vision y se queda sin el dato con el que
    tenía que medir. El síntoma llega tarde y despistado: el barrido alquila una
    máquina, la paga, y el benchmark se para diciendo "falta la fuente".

    Va por SSH y por stdin, como el resto: nada aparece en el `ps` del destino.
    El tar se arma con `tarfile` en vez de llamar a `tar` para que el comando sea
    el mismo desde Windows que desde Linux.

    Lo que se sube queda con dueño DO_DEV_USER, que es quien luego lo usa; si se
    dejara de root, el benchmark fallaría por permisos a mitad del alquiler.
    """
    origen = Path(args.origen).expanduser()
    if not origen.is_dir():
        die(f"No existe el directorio {origen}")
    destino = (args.destino or f"src/{origen.name}").strip().strip("/")
    if not destino:
        die("El destino no puede estar vacío: es una ruta dentro del home.")

    excluidos = set(EXCLUIDOS_POR_DEFECTO) | {
        e.strip() for bruto in args.exclude for e in bruto.split(",") if e.strip()
    }
    dev_user = cfg("DO_DEV_USER")
    droplet, ip, port = resolve_target(args.name or "", args.port or 0)

    def filtro(info: tarfile.TarInfo) -> "tarfile.TarInfo | None":
        partes = Path(info.name).parts[1:]  # sin el nombre de la raíz del tar
        return None if any(p in excluidos for p in partes) else info

    tmp = Path(tempfile.mkdtemp(prefix="do-push-dir-")) / "payload.tar.gz"
    log(f"Empaquetando {origen} (sin {', '.join(sorted(excluidos))})…")
    with tarfile.open(tmp, "w:gz") as tar:
        tar.add(str(origen), arcname="contenido", filter=filtro)
    megas = tmp.stat().st_size / 1e6
    log(f"  {megas:.1f} MB. Subiendo a '{droplet['name']}' ({ip}:{port}) → ~{dev_user}/{destino}")

    with tmp.open("rb") as fh:
        proc = subprocess.run(
            ssh_command(ip, port, user="root") + ["cat > /tmp/push-dir.tar.gz"],
            stdin=fh,
        )
    if proc.returncode != 0:
        die("No pude subir el paquete. La salida de ssh está justo arriba.")

    script = "\n".join(
        [
            "set -eu",
            f"DEV_USER={shq(dev_user)}",
            'H=$(getent passwd "$DEV_USER" | cut -d: -f6)',
            '[ -n "$H" ] || { echo "no existe el usuario $DEV_USER" >&2; exit 1; }',
            f"DEST=\"$H/{destino}\"",
            'install -d -o "$DEV_USER" -g "$DEV_USER" "$(dirname "$DEST")"',
            'install -d -o "$DEV_USER" -g "$DEV_USER" "$DEST"',
            # --strip-components quita el "contenido/" del tar, así que lo que
            # cae en DEST es el contenido del directorio y no un nivel de más.
            'tar -xzf /tmp/push-dir.tar.gz -C "$DEST" --strip-components=1',
            'chown -R "$DEV_USER:$DEV_USER" "$DEST"',
            "rm -f /tmp/push-dir.tar.gz",
            'echo "  $(find "$DEST" -type f | wc -l) ficheros en $DEST"',
        ]
    )
    if run_remote_script(ip, port, script) != 0:
        die("El paquete subió pero no se pudo desempaquetar.")
    log("Listo.")


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


ENTORNOS_DIR = ROOT / "entornos"


def load_entorno(name: str) -> dict:
    """Lee entornos/<nombre>.json: el .env de UN proyecto, declarado.

    Dato, no codigo, como types/ y services/. Resuelve el problema de que varios
    proyectos tengan cada uno su .env y haya que llevarlos de una maquina a otra
    en los dos sentidos.

    La forma es la que evita la trampa: el .env NO se copia entre maquinas, se
    GENERA del llavero. Copiarlo exigiria saber cual de las dos copias es la
    buena, y no hay forma de saberlo -las fechas mienten: el .env de un dev
    recien nacido es el mas NUEVO y el mas VACIO-. Ademas es la forma exacta del
    fallo que ya pago este repo: `provision` reescribe dev-secrets.env con
    `cat >`, asi que emitir desde una maquina a la que le falta un token lo BORRA
    en el destino.

    Y como se regenera, perder un .env no cuesta nada; por eso nadie tiene la
    tentacion de commitearlo "por si acaso", que es de donde salen la mitad de
    los secretos filtrados.
    """
    path = ENTORNOS_DIR / f"{name}.json"
    if not path.exists():
        disponibles = ", ".join(sorted(p.stem for p in ENTORNOS_DIR.glob("*.json")))
        die(
            f"No existe el entorno '{name}' (falta {path}).\n"
            f"  Definidos: {disponibles or 'ninguno'}"
        )
    try:
        ent = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        die(f"{path} no es JSON valido: {exc}")
    if not ent.get("dir"):
        die(f"{path}: falta el campo obligatorio 'dir'.")
    ent["name"] = name
    ent.setdefault("fichero", ".env")
    ent.setdefault("variables", [])
    for var in ent["variables"]:
        if not var.get("nombre"):
            die(f"{path}: cada variable necesita 'nombre'.")
        # `desde` es el nombre en el LLAVERO; `nombre`, el de dentro del .env del
        # proyecto. Son distintos a proposito y no es burocracia: la key de
        # Tailscale se llama CWEB_TS_AUTHKEY en el llavero y TS_AUTHKEY dentro
        # del proyecto, y el nombre de destino lo declara quien la CONSUME.
        var.setdefault("desde", var["nombre"])
        var.setdefault("obligatoria", False)
        var.setdefault("porque", "")
    return ent


def all_entornos() -> list[dict]:
    if not ENTORNOS_DIR.exists():
        return []
    return [load_entorno(p.stem) for p in sorted(ENTORNOS_DIR.glob("*.json"))]


def git_ignora(repo: Path, fichero: str) -> bool | None:
    """Si git ignora ese fichero en ese repo. None = no hay repo que preguntar.

    Es la red contra las filtraciones, y es ESTRUCTURAL y no disciplinaria:
    antes de escribir un .env se le pregunta a git si lo ignora, y si no, no se
    escribe. Falla en el momento en que se comete el error y no en el `git push`
    de dentro de tres semanas, y no depende de que nadie recuerde nada.

    No es teorico: el 2026-09-10, `claude-code-webapp-mobile` -repo PUBLICO cuyo
    .gitignore entero era `node_modules/`- leia TS_AUTHKEY de un .env. El
    fichero no existia aun; el dia que existiera, git lo habria rastreado.
    """
    if not (repo / ".git").exists():
        return None
    proc = subprocess.run(
        ["git", "-C", str(repo), "check-ignore", "-q", fichero],
        capture_output=True,
    )
    # 0 = ignorado, 1 = NO ignorado, 128 = no es un repo.
    return proc.returncode == 0 if proc.returncode in (0, 1) else None


def cmd_entornos(args: argparse.Namespace) -> None:
    if args.accion == "list":
        return _entornos_list()
    base = dentro_del_droplet("entornos " + args.accion)
    if args.accion == "aplicar":
        return _entornos_aplicar(base, args)
    return _entornos_comprobar(base, args)


def _entornos_list() -> None:
    entornos = all_entornos()
    if not entornos:
        log("No hay entornos declarados en entornos/.")
        return
    for ent in entornos:
        obl = sum(1 for v in ent["variables"] if v["obligatoria"])
        log(f"\n{ent['name']}  ->  ~/src/{ent['dir']}/{ent['fichero']}")
        if ent.get("descripcion"):
            log(f"  {ent['descripcion']}")
        log(f"  {len(ent['variables'])} variables ({obl} obligatorias)")
        for var in ent["variables"]:
            flecha = "" if var["desde"] == var["nombre"] else f"  <- {var['desde']}"
            marca = "*" if var["obligatoria"] else " "
            log(f"    {marca} {var['nombre']}{flecha}")


def _entornos_aplicar(base: Path, args: argparse.Namespace) -> None:
    """Regenera los .env de los proyectos declarados, desde el llavero."""
    entornos = all_entornos()
    if args.entorno:
        entornos = [e for e in entornos if e["name"] in args.entorno]
    if not entornos:
        log("No hay entornos que aplicar.")
        return

    # Dos entornos que escriben el MISMO fichero se pisan, y el que gana depende
    # del orden alfabetico: `telegram-coordinator` y `telegram-launcher` son el
    # mismo repo con otro bot, asi que aplicarlos juntos dejaria al mini con el
    # bot del dev o al reves, y el sintoma seria un 409 en el que nadie pensaria.
    # Mismo criterio que `selected_services`, que ya rechaza dos servicios del
    # mismo directorio: se para y se pide que elijan.
    por_destino: dict[str, str] = {}
    for ent in entornos:
        clave = f"{ent['dir']}/{ent['fichero']}"
        otro = por_destino.get(clave)
        if otro:
            die(
                f"'{otro}' y '{ent['name']}' escriben los dos en ~/src/{clave}.\n"
                "  El segundo pisaria al primero, y cual gana dependeria del orden\n"
                "  alfabetico. Elige uno:\n"
                f"    entornos aplicar --entorno {otro}\n"
                f"    entornos aplicar --entorno {ent['name']}"
            )
        por_destino[clave] = ent["name"]

    escritos = fallos = 0
    for ent in entornos:
        repo = base / ent["dir"]
        destino = repo / ent["fichero"]
        if not repo.is_dir():
            log(f"  {ent['name']}: no existe {repo}, me lo salto")
            continue

        ignorado = git_ignora(repo, ent["fichero"])
        if ignorado is False:
            log(
                f"  {ent['name']}: NO ESCRITO. git NO ignora {ent['fichero']} en "
                f"{repo}.\n"
                f"    Escribirlo ahi lo pondria en camino de un commit. Arregla su\n"
                f"    .gitignore primero:  echo '{ent['fichero']}' >> {repo}/.gitignore"
            )
            fallos += 1
            continue

        lineas, faltan = [], []
        for var in ent["variables"]:
            valor = os.environ.get(var["desde"], "").strip()
            if valor:
                lineas.append(f"{var['nombre']}={valor}")
            elif var["obligatoria"]:
                faltan.append(f"{var['nombre']} (desde {var['desde']})")

        if faltan:
            log(f"  {ent['name']}: faltan obligatorias: {', '.join(faltan)}")
            fallos += 1
        if not lineas:
            log(f"  {ent['name']}: ninguna variable con valor, no se escribe nada")
            continue

        cabecera = [
            "# Generado por do_droplet.py entornos aplicar. NO lo edites a mano:",
            "# se regenera del llavero (~/.config/dev-secrets.env) y se pierde.",
        ]
        destino.write_text("\n".join(cabecera + lineas) + "\n", encoding="utf-8")
        destino.chmod(0o600)
        escritos += 1
        log(f"  {ent['name']}: {len(lineas)} variables en {destino}")

    log(f"\n{escritos} .env escrito(s), {fallos} con problemas.")
    if fallos:
        raise SystemExit(1)


def _entornos_comprobar(base: Path, args: argparse.Namespace) -> None:
    """Audita los .env declarados contra git: ignorados AHORA y en la HISTORIA."""
    problemas = 0
    for ent in all_entornos():
        repo = base / ent["dir"]
        if not repo.is_dir():
            continue
        ignorado = git_ignora(repo, ent["fichero"])
        # La historia importa aparte: un secreto commiteado una vez y borrado
        # despues SIGUE en la historia y sigue filtrado. Borrar el fichero no lo
        # arregla; hay que rotar el secreto.
        proc = subprocess.run(
            ["git", "-C", str(repo), "log", "--all", "--oneline", "--", ent["fichero"]],
            capture_output=True, text=True,
        )
        historia = [l for l in proc.stdout.splitlines() if l.strip()]
        estado = {True: "ignorado", False: "NO IGNORADO", None: "no es repo git"}[ignorado]
        log(f"{ent['name']:<28} {ent['fichero']:<12} {estado}")
        if ignorado is False:
            problemas += 1
        if historia:
            log(f"    ⚠ ESTUVO COMMITEADO en {len(historia)} commit(s). Rota el secreto:")
            for linea in historia[:5]:
                log(f"      {linea}")
            problemas += 1
    if problemas:
        die(f"\n{problemas} problema(s). Un .env en git es un secreto filtrado.")
    log("\nTodos los .env declarados estan fuera de git, ahora y en la historia.")


def cmd_llavero(args: argparse.Namespace) -> None:
    """Mueve el llavero entre maquinas, en los dos sentidos y sin borrar nada.

    Las dos reglas que hacen seguro el "en los dos sentidos", y que son el
    motivo de que no exista un `sync`:

    1. `traer` solo ANADE: nunca pisa un valor que aqui ya exista, salvo
       `--pisar NOMBRE`. La union crece y nada desaparece, asi que un dev recien
       nacido y vacio no puede hacer dano: no tiene nada que imponer.
    2. La direccion la eliges tu. Un `sync` que decide solo es un `sync` que un
       dia decide mal, y con secretos eso no se nota hasta que algo deja de
       autenticar.

    Y ninguno de los dos BORRA jamas: quitar una variable es `llavero olvidar`,
    un comando aparte, para que borrar no pueda ser efecto secundario de
    sincronizar.
    """
    if args.accion == "comparar":
        return _llavero_comparar(args)
    if args.accion == "enviar":
        return _llavero_enviar(args)
    if args.accion == "traer":
        return _llavero_traer(args)
    return _llavero_olvidar(args)


def _llavero_estado(args: argparse.Namespace):
    variables = cargar_llavero()
    dev_user = cfg("DO_DEV_USER")
    droplet, ip, port = resolve_target(args.maquina or "", args.port or 0)
    alli = _estado_llavero_remoto(ip, port, dev_user)
    aqui = {v["nombre"] for v in variables if os.environ.get(v["nombre"], "").strip()}
    return variables, droplet, ip, port, aqui, alli


def _llavero_comparar(args: argparse.Namespace) -> None:
    variables, droplet, _ip, _port, aqui, alli = _llavero_estado(args)
    obligatorias = {v["nombre"] for v in variables if v["obligatoria"]}
    nombre = droplet["name"]
    log(f"Comparando el llavero de ESTA maquina con el de '{nombre}'.")
    log("Solo NOMBRES: ningun valor se lee ni se imprime.\n")
    log(f"  {'variable':<28} {'aqui':<6} {nombre}")
    for var in variables:
        n = var["nombre"]
        marca = "*" if var["obligatoria"] else " "
        log(f"  {marca}{n:<27} {'si' if n in aqui else '--':<6} "
            f"{'si' if n in alli else '--'}")
    solo_aqui = sorted(aqui - alli)
    solo_alli = sorted(alli & {v['nombre'] for v in variables} - aqui)
    log("")
    if solo_aqui:
        log(f"  Solo aqui  ({len(solo_aqui)}): {', '.join(solo_aqui)}")
        log(f"    -> llavero enviar {nombre}")
    if solo_alli:
        log(f"  Solo alli  ({len(solo_alli)}): {', '.join(solo_alli)}")
        log(f"    -> llavero traer {nombre}")
    if not solo_aqui and not solo_alli:
        log("  Iguales. No hay nada que mover.")
    faltan = sorted(obligatorias - aqui - alli)
    if faltan:
        log(f"\n  ⚠ No estan en NINGUNA de las dos: {', '.join(faltan)}")
        log("    Eso no lo arregla mover nada; hay que reemitirlas. Manual:")
        log("    estudios-redes-neuronales/docs/secretos-desde-cero.md")


def _llavero_enviar(args: argparse.Namespace) -> None:
    args.llavero = True
    args.name = args.maquina
    args.vars = []
    cmd_push_secret(args)


def _llavero_traer(args: argparse.Namespace) -> None:
    """Trae al .env de ESTA maquina las variables que solo estan alli.

    Es el unico camino que LEE valores de otra maquina, y por eso solo anade.
    """
    variables, droplet, ip, port, aqui, alli = _llavero_estado(args)
    conocidas = {v["nombre"] for v in variables}
    pisar = set(push_env_names(args.pisar or []))
    candidatas = sorted((alli & conocidas) - aqui | (pisar & alli))
    if not candidatas:
        log(f"'{droplet['name']}' no tiene nada del llavero que aqui falte.")
        return

    dev_user = cfg("DO_DEV_USER")
    script = "\n".join(
        [
            "set -eu",
            f"DEV_USER={shq(dev_user)}",
            'H=$(getent passwd "$DEV_USER" | cut -d: -f6)',
            'F="$H/.config/dev-secrets.env"',
            '[ -f "$F" ] || { echo "no hay llavero alli" >&2; exit 1; }',
        ]
        + [f'grep "^export {n}=" "$F" || true' for n in candidatas]
    )
    code, salida, err = run_remote_split(ip, port, script)
    if code != 0:
        die(f"No pude leer el llavero de '{droplet['name']}': {err.strip()}")

    env_file = ROOT / ".env"
    texto = env_file.read_text(encoding="utf-8") if env_file.exists() else ""
    anadidas = []
    for linea in salida.splitlines():
        linea = linea.strip()
        if not linea.startswith("export "):
            continue
        cuerpo = linea[len("export "):]
        nombre = cuerpo.split("=", 1)[0]
        valor = cuerpo.split("=", 1)[1].strip()
        # El origen entrecomilla con shq; el .env de aqui no lleva comillas.
        if len(valor) >= 2 and valor[0] == valor[-1] == "'":
            valor = valor[1:-1].replace("'\"'\"'", "'")
        if nombre in aqui and nombre not in pisar:
            continue
        texto = "\n".join(
            l for l in texto.splitlines() if not l.startswith(f"{nombre}=")
        )
        texto = texto.rstrip("\n") + f"\n{nombre}={valor}\n"
        anadidas.append(nombre)

    if not anadidas:
        log("No vino ninguna variable utilizable.")
        return
    env_file.write_text(texto, encoding="utf-8")
    log(f"Traidas de '{droplet['name']}' al .env de aqui: {', '.join(anadidas)}")
    log("  (solo se anaden; para pisar una que ya estaba: --pisar NOMBRE)")


def _llavero_olvidar(args: argparse.Namespace) -> None:
    """Quita variables del llavero de una maquina. Comando APARTE a proposito.

    Borrar nunca puede ser efecto secundario de sincronizar: si `traer` o
    `enviar` pudieran quitar cosas, un dia una maquina a medias vaciaria a la
    otra y el sintoma llegaria dias despues.
    """
    nombres = push_env_names(args.pisar or []) or push_env_names(args.vars or [])
    if not nombres:
        die("Dime que variables olvidar: llavero olvidar <maquina> VAR[,VAR2]")
    droplet, ip, port = resolve_target(args.maquina or "", args.port or 0)
    log(f"Quitando de '{droplet['name']}': {', '.join(nombres)}")
    if not args.yes and not confirmar("Escribe 'si' para confirmar: "):
        log("No se toca nada.")
        return
    dev_user = cfg("DO_DEV_USER")
    lineas = [
        "set -eu",
        "umask 077",
        f"DEV_USER={shq(dev_user)}",
        'H=$(getent passwd "$DEV_USER" | cut -d: -f6)',
        'F="$H/.config/dev-secrets.env"',
        '[ -f "$F" ] || exit 0',
    ]
    for nombre in nombres:
        lineas += [
            f'grep -v "^export {nombre}=" "$F" > "$F.tmp" || true',
            'mv "$F.tmp" "$F"',
        ]
    lineas += [
        'chmod 600 "$F"',
        'chown "$DEV_USER:$DEV_USER" "$F"',
        'echo "  quedan $(grep -c ^export "$F") variables"',
    ]
    if run_remote_script(ip, port, "\n".join(lineas)) != 0:
        die("Fallo al quitarlas.")
    log("Listo.")


# Raiz del repo del lanzador DENTRO de un droplet. Los comandos "de dentro" se
# ejecutan desde ahi, porque el coordinador tambien lo hace asi y porque los
# ficheros de datos (types/, services/, llavero.json) se leen relativos a ROOT.
REPO_DENTRO = "~/src/digital-ocean-dropplet-auto-launching"


def cmd_remoto(args: argparse.Namespace) -> None:
    """Pide DESDE FUERA un comando de los que actuan DENTRO de una maquina.

    `update`, `install-service`, `install-executors` y `entornos aplicar` actuan
    sobre la maquina donde corren, y `dentro_del_droplet()` se niega a
    ejecutarlos en la laptop -con razon-. Hasta ahora la unica forma de
    pedirselos a OTRA maquina era teclear a mano la version larga:

        ssh mini --cmd 'cd ~/src/digital-ocean... && python3 scripts/do_droplet.py update'

    que es exactamente la clase de linea que desde el movil se teclea mal.

    Corre como DO_DEV_USER y con `bash -lc`, y el shell de LOGIN no es adorno:
    sin el no se carga `dev-secrets.env` y el comando de dentro falla con un
    "falta el token" en una maquina donde el token si esta. Es el mismo mecanismo
    que `ejecutar_post()` y por el mismo motivo.

    Devuelve el codigo de salida de dentro para que se pueda encadenar, y para
    que el bot publique el stderr cuando algo falla: un `remoto` que siempre sale
    con 0 convierte un fallo remoto en un silencio.
    """
    if not args.comando:
        die(
            "Dime que comando ejecutar alli. Por ejemplo:\n"
            "  python scripts/do_droplet.py remoto mini update\n"
            "  python scripts/do_droplet.py remoto dev entornos aplicar"
        )
    droplet, ip, port = resolve_target(args.maquina, args.port or 0)
    dev_user = cfg("DO_DEV_USER")
    dentro = " ".join(shq(a) for a in args.comando)
    orden = f"cd {REPO_DENTRO} && python3 scripts/do_droplet.py {dentro}"
    log(f"En '{droplet['name']}' ({ip}:{port}), como {dev_user}:\n  $ {orden}\n")

    script = "\n".join(
        [
            "set -eu",
            f"DEV_USER={shq(dev_user)}",
            f'sudo -u "$DEV_USER" -H bash -lc {shq(orden)}',
        ]
    )
    code = run_remote_script(ip, port, script)
    if code != 0:
        die(
            f"El comando fallo en '{droplet['name']}' (codigo {code}). Su salida "
            "esta justo arriba."
        )
    log(f"\nListo en '{droplet['name']}'.")


def _estado_llavero_remoto(ip: str, port: int, dev_user: str) -> set[str]:
    """Que variables del llavero tiene una maquina. NOMBRES, nunca valores.

    Se pregunta a la maquina en vez de deducirlo de su tipo porque lo que importa
    es lo que hay, no lo que deberia haber: el mini del 2026-09-10 llevaba 19
    variables y su tipo declaraba una.
    """
    script = "\n".join(
        [
            "set -eu",
            f"DEV_USER={shq(dev_user)}",
            'H=$(getent passwd "$DEV_USER" | cut -d: -f6)',
            'F="$H/.config/dev-secrets.env"',
            '[ -f "$F" ] || exit 0',
            # Solo el nombre, y solo si tiene valor: una linea `export X=` vacia
            # es lo mismo que no tenerla y contarla mentiria.
            'grep -oE "^export [A-Za-z_][A-Za-z0-9_]*=." "$F" | '
            "sed \"s/^export //;s/=.$//\" || true",
        ]
    )
    code, salida, _ = run_remote_split(ip, port, script)
    if code != 0:
        return set()
    return {l.strip() for l in salida.splitlines() if l.strip()}


def cmd_flota(args: argparse.Namespace) -> None:
    """Comprueba la PARIDAD de las maquinas de la flota. Solo lectura.

    Contesta de una vez las tres preguntas que hasta ahora habia que ir a mirar
    a mano a cada maquina, y que son justo las que se descubren tarde:

      1. Que le falta del llavero (por NOMBRE; ningun valor se imprime).
      2. Si tiene la clave de la flota, o sea si las demas pueden entrar.
      3. Que servicios corre de verdad, preguntandoselo a systemd.

    Sale != 0 si a alguna le falta algo obligatorio, para poder encadenarlo y
    para que el bot publique el motivo: con codigo 0 el coordinador no publica
    stderr y el aviso no llega al chat.
    """
    variables = cargar_llavero()
    obligatorias = {v["nombre"] for v in variables if v["obligatoria"]}
    todas = {v["nombre"] for v in variables}
    dev_user = cfg("DO_DEV_USER")
    destino = FICHERO_CLAVE_FLOTA.replace("~/", "")

    droplets = find_droplets(tag=args.tag) if args.tag else find_droplets()
    if args.maquina:
        droplets = [d for d in droplets if d["name"] == args.maquina]
    if not droplets:
        log("No hay droplets vivos que mirar.")
        return

    problemas = 0
    for droplet in droplets:
        nombre = droplet["name"]
        ip = public_ip(droplet)
        log(f"\n=== {nombre}  ({ip})")
        if not ip:
            log("  sin IP publica, no se puede preguntar nada")
            problemas += 1
            continue
        port = wait_for_ssh(ip, timeout=25) or 0
        if not port:
            log(f"  no contesta por SSH en {cfg('DO_SSH_PORTS')}: no se puede comprobar")
            problemas += 1
            continue

        tiene = _estado_llavero_remoto(ip, port, dev_user)
        faltan_obl = sorted(obligatorias - tiene)
        faltan_opt = sorted(todas - obligatorias - tiene)
        log(f"  llavero      {len(tiene & todas)}/{len(todas)}")
        if faltan_obl:
            log(f"    FALTAN obligatorias: {', '.join(faltan_obl)}")
            problemas += 1
        if faltan_opt:
            log(f"    faltan opcionales:   {', '.join(faltan_opt)}")

        script = "\n".join(
            [
                "set -eu",
                f"DEV_USER={shq(dev_user)}",
                'H=$(getent passwd "$DEV_USER" | cut -d: -f6)',
                f'[ -f "$H/{destino}" ] && echo CLAVE_SI || echo CLAVE_NO',
                # systemd contesta a cualquier usuario, y los ficheros de unidad
                # quedan en modo 600 de root: leerlos daria lista vacia.
                "systemctl list-units --type=service --no-legend --plain "
                "--state=running | awk '{print $1}' | sed 's/.service$//' || true",
            ]
        )
        code, salida, _ = run_remote_split(ip, port, script)
        lineas = [l.strip() for l in salida.splitlines() if l.strip()]
        clave = "si" if "CLAVE_SI" in lineas else "NO"
        if clave == "NO":
            problemas += 1
        servicios = [
            l for l in lineas
            if l not in ("CLAVE_SI", "CLAVE_NO") and ("telegram" in l or "web" in l)
        ]
        log(f"  clave flota  {clave}")
        log(f"  servicios    {', '.join(servicios) or 'ninguno de la flota'}")

    if problemas:
        die(
            f"\n{problemas} problema(s) de paridad. Lo que suele arreglarlos:\n"
            "  python scripts/do_droplet.py push-secret --llavero --name <maquina>\n"
            "  python scripts/do_droplet.py autorizar-flota <maquina>"
        )
    log(f"\nParidad correcta en {len(droplets)} maquina(s).")


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

    p = sub.add_parser("keys", help="claves SSH registradas en tu cuenta")
    p.add_argument(
        "--prune",
        metavar="PATRON",
        default="",
        help="borra de la cuenta las claves cuyo NOMBRE encaje (p. ej. "
        "'lanzador-*'). Nunca toca la de la flota ni la de esta maquina. "
        "Borrarlas no echa a nadie de una maquina que ya existe",
    )
    p.add_argument("--yes", action="store_true", help="no preguntar antes de borrar")
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
    p.add_argument(
        "--sin-github",
        action="store_true",
        help="no enviar ningún token de GitHub, y no comprobarlo. La salida de "
        "emergencia cuando el token está caducado y aun así hace falta la "
        "máquina: nacerá sin los repos privados",
    )
    p.add_argument(
        "--llavero",
        action="store_true",
        help="manda el llavero entero (llavero.json) a la maquina. Los tipos de "
        "la flota lo piden solos con \"llavero\": true; esto es para una maquina "
        "que no tiene tipo",
    )
    p.add_argument(
        "--sin-llavero",
        action="store_true",
        help="no morir si faltan secretos obligatorios del llavero. Salida de "
        "emergencia, como --sin-github: la maquina nacera coja y se dira",
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
    p.add_argument(
        "--sin-github",
        action="store_true",
        help="no enviar ningún token de GitHub, y no comprobarlo. La salida de "
        "emergencia cuando el token está caducado y aun así hace falta la "
        "máquina: nacerá sin los repos privados",
    )
    p.add_argument(
        "--llavero",
        action="store_true",
        help="manda el llavero entero (llavero.json) a la maquina. Los tipos de "
        "la flota lo piden solos con \"llavero\": true; esto es para una maquina "
        "que no tiene tipo",
    )
    p.add_argument(
        "--sin-llavero",
        action="store_true",
        help="no morir si faltan secretos obligatorios del llavero. Salida de "
        "emergencia, como --sin-github: la maquina nacera coja y se dira",
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
        "push-github-token",
        help="rota el token de GitHub de un droplet ya creado en sus tres sitios "
        "(dev-secrets.env, .git-credentials y gh) sin tocar sus demas secretos. "
        "push-secret solo llega al primero y deja a git con el viejo",
    )
    p.add_argument("name", nargs="?")
    p.add_argument("--port", type=int)
    p.add_argument(
        "--from-env",
        default="GITHUB_TOKEN",
        metavar="VAR",
        help="variable de ESTA maquina cuyo valor se envia como GITHUB_TOKEN",
    )
    p.set_defaults(func=cmd_push_github_token)

    p = sub.add_parser(
        "push-service-env",
        help="añade o rota variables en el .env de un servicio sin borrar el "
        "resto (a diferencia de provision, que lo reescribe entero)",
    )
    p.add_argument("service", help="nombre del descriptor en services/")
    p.add_argument(
        "vars",
        nargs="+",
        help="variables a enviar, separadas por coma. Se admite el nombre de "
        "destino (VAST_AI_API_TOKEN) o el de origen (TGL_VAST_AI_API_TOKEN)",
    )
    p.add_argument("--name", help="droplet, si no es el de .env")
    p.add_argument("--port", type=int)
    p.set_defaults(func=cmd_push_service_env)

    p = sub.add_parser(
        "executors",
        help="catálogo de ejecutores del bot con ejemplos, leyendo "
        "<repo>/telegram/executors/ de cada repo",
    )
    p.add_argument(
        "--dir",
        action="append",
        default=[],
        help="raíz donde buscar repos (repetible). Por defecto ~/src",
    )
    p.set_defaults(func=cmd_executors)

    p = sub.add_parser(
        "push-secret",
        help="escribe variables en los secretos de la máquina (dev-secrets.env) "
        "sin borrar las demás. A diferencia del .env de un servicio, esto lo "
        "ven también las sesiones SSH, no sólo el bot",
    )
    p.add_argument(
        "vars",
        nargs="*",
        default=[],
        help="nombres de variables, separadas por coma. Vacío si usas --llavero",
    )
    p.add_argument(
        "--llavero",
        action="store_true",
        help="manda el LLAVERO ENTERO (llavero.json) en vez de unas pocas. Es el "
        "camino de reparación de una máquina viva: mezcla, no reescribe, así que "
        "no borra del destino lo que esta máquina no tenga a mano",
    )
    p.add_argument(
        "--sin-llavero",
        action="store_true",
        help="con --llavero, no morir si faltan obligatorias aquí",
    )
    p.add_argument(
        "--prefix",
        default="TGL_",
        help="prefijo con el que buscarlas en tu .env si no están sin él "
        "(por defecto TGL_, el del bot Lanzador)",
    )
    p.add_argument("--name", help="droplet, si no es el de .env")
    p.add_argument("--port", type=int)
    p.set_defaults(func=cmd_push_secret)

    p = sub.add_parser(
        "install-executors",
        help="DENTRO de una máquina: aplica los ficheros que declara un "
        "servicio (ejecutores del bot) sin tener que reaprovisionar",
    )
    p.add_argument(
        "--service",
        action="append",
        default=[],
        help="servicio de services/ (repetible). Por defecto, DO_SERVICES",
    )
    p.set_defaults(func=cmd_install_executors)

    p = sub.add_parser(
        "install-service",
        help="DENTRO de una máquina: instala un servicio de services/ que "
        "todavía no está, sin rehacer el droplet",
    )
    p.add_argument(
        "--service",
        action="append",
        default=[],
        help="servicio de services/ (repetible). Por defecto, DO_SERVICES",
    )
    p.add_argument(
        "--timeout",
        type=int,
        default=3600,
        help="segundos máximos (un npm ci o un venv de torch son minutos)",
    )
    p.set_defaults(func=cmd_install_service)

    p = sub.add_parser(
        "push-dir",
        help="copia un directorio local al droplet, para lo que git no trae "
        "(datasets gitignoreados, ficheros grandes)",
    )
    p.add_argument("origen", help="directorio de ESTA máquina")
    p.add_argument(
        "destino",
        nargs="?",
        help="ruta dentro del home del usuario de desarrollo "
        "(por defecto src/<nombre del directorio>)",
    )
    p.add_argument("--name", help="droplet, si no es el de .env")
    p.add_argument("--port", type=int)
    p.add_argument(
        "--exclude",
        action="append",
        default=[],
        metavar="NOMBRES",
        help="nombres a excluir, separados por coma. Se suman a los de siempre "
        "(.git, .venv, node_modules, __pycache__)",
    )
    p.set_defaults(func=cmd_push_dir)

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

    p = sub.add_parser(
        "clave-flota",
        help="crea y registra LA clave compartida de la flota. Una sola, "
        "registrada una vez: cualquier droplet creado despues la acepta, y con "
        "eso el acceso entre maquinas deja de depender de cual nacio primero",
    )
    p.set_defaults(func=cmd_clave_flota)

    p = sub.add_parser(
        "autorizar-flota",
        help="autoriza la clave de la flota DENTRO de una maquina que ya existia "
        "(para root y para el usuario de desarrollo). Es la reparacion de las "
        "maquinas nacidas antes que la clave",
    )
    p.add_argument("name", nargs="?")
    p.add_argument("--port", type=int)
    p.add_argument(
        "--usuario",
        action="append",
        default=[],
        help="usuario del destino (repetible). Por defecto root y DO_DEV_USER, "
        "y las dos hacen falta: el aprovisionamiento entra siempre como root",
    )
    p.add_argument(
        "--solo-publica",
        action="store_true",
        help="no mandar la clave PRIVADA. La maquina dejara ENTRAR a la flota "
        "pero no podra SALIR hacia las demas, que es media paridad",
    )
    p.set_defaults(func=cmd_autorizar_flota)

    p = sub.add_parser(
        "remoto",
        help="pide DESDE FUERA un comando de los que actuan DENTRO de una "
        "maquina (update, install-service, entornos aplicar...)",
    )
    p.add_argument("maquina", help="nombre del droplet")
    p.add_argument(
        "comando",
        nargs=argparse.REMAINDER,
        help="el comando de do_droplet.py y sus argumentos, tal cual",
    )
    p.add_argument("--port", type=int)
    p.set_defaults(func=cmd_remoto)

    p = sub.add_parser(
        "flota",
        help="comprueba la PARIDAD de las maquinas vivas: que les falta del "
        "llavero, si tienen la clave de la flota y que servicios corren. Solo "
        "lectura, y no imprime ningun valor",
    )
    p.add_argument("maquina", nargs="?", help="solo esta; por defecto, todas")
    p.add_argument("--tag", default="", help="solo las de este tag")
    p.set_defaults(func=cmd_flota)

    p = sub.add_parser(
        "entornos",
        help="el .env de cada proyecto, declarado en entornos/ y GENERADO del "
        "llavero en vez de copiado entre maquinas",
    )
    p.add_argument(
        "accion",
        choices=["list", "aplicar", "comprobar"],
        help="list: que hay declarado (vale desde la laptop). aplicar y "
        "comprobar corren DENTRO de una maquina",
    )
    p.add_argument(
        "--entorno",
        action="append",
        default=[],
        help="solo estos entornos (repetible). Por defecto, todos",
    )
    p.set_defaults(func=cmd_entornos)

    p = sub.add_parser(
        "llavero",
        help="mueve el llavero entre maquinas en los dos sentidos. traer solo "
        "ANADE y enviar solo MEZCLA: ningun camino borra nada",
    )
    p.add_argument("accion", choices=["comparar", "enviar", "traer", "olvidar"])
    p.add_argument("maquina", nargs="?", help="droplet, si no es el de .env")
    p.add_argument(
        "vars",
        nargs="*",
        default=[],
        help="con 'olvidar', que variables quitar",
    )
    p.add_argument(
        "--pisar",
        action="append",
        default=[],
        metavar="VARS",
        help="con 'traer', variables cuyo valor local SI se sobrescribe",
    )
    p.add_argument("--sin-llavero", action="store_true")
    p.add_argument("--prefix", default="TGL_")
    p.add_argument("--yes", action="store_true")
    p.add_argument("--port", type=int)
    p.set_defaults(func=cmd_llavero)

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
