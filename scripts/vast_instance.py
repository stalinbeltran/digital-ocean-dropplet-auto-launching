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
PERFIL_DIR = ROOT / "vast-perfiles"
RESULT_DIR = ROOT / "results"
# El registro de maquinas que fallaron. Es DATO commiteado, no cache: la maquina
# de control es efimera (se rehace sin aviso) y lo que no esta en el remoto no
# existe, asi que un bloqueo aprendido pagando se perderia con ella.
BLOQUEADAS = ROOT / "vast-bloqueadas.json"
# CUANTO DURA UN BLOQUEO, y por que lleva fecha de caducidad escrita aqui al
# lado: en Vast se alquila a hosts de desconocidos que se arreglan, se actualizan
# y cambian de dueno. Un bloqueo eterno solo crece, y a base de crecer deja el
# mercado sin ofertas sin decir por que. 30 dias desde el ULTIMO fallo: se
# olvida solo, y si vuelve a fallar se renueva solo tambien.
BLOQUEO_DIAS = 30
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
    min_price: float = 0.0,
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

    ⚠ **Devuelve como mucho 64 ofertas, y son las 64 MÁS BARATAS** (el orden es
    `dph_total asc`). No es un `limit` que se pueda subir: la API corta ahí. Para
    ver el catálogo entero hace falta `buscar_ofertas_paginado`, que recorre el
    precio por tramos — ver allí por qué importa tanto.
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
    precio: dict = {}
    if min_price:
        precio["gte"] = min_price
    if max_price:
        precio["lte"] = max_price
    if precio:
        consulta["dph_total"] = precio

    resp = api("POST", "/api/v0/bundles/", consulta)
    ofertas = resp.get("offers") if isinstance(resp, dict) else None
    return [o for o in (ofertas or []) if o.get("resource_type") == "gpu"]


# Tope duro de la API por peticion. No es configurable: pedir mas no da mas.
TOPE_API = 64
# Ancho del tramo de vCPU con que se recorre el catalogo (ver
# buscar_ofertas_paginado). 1 es lo mas fino que tiene sentido: `cpu_cores_effective`
# es fraccionario pero se agrupa en enteros.
PASO_VCPU = 1
# Cuanto vale una lectura del catalogo antes de repetirla. Un recorrido completo
# son ~24 peticiones (~10 s), y el reparto de una flota pide ofertas una vez por
# lote y por reintento: sin cache eso multiplica el recorrido por cada lote. Con
# cache eterna, en cambio, una maquina liberada hace un minuto no se veria nunca.
# 60 s es el compromiso, y la regla va escrita aqui al lado a proposito.
CACHE_TTL_S = 60.0
_cache_catalogo: dict = {}


def buscar_ofertas_paginado(
    cpus: int = 0,
    max_cpus: int = 0,
    min_ram_gb: float = 0.0,
    max_price: float = 0.0,
    usar_cache: bool = True,
) -> list[dict]:
    """El catalogo ENTERO, recorriendolo por tramos de vCPU. Ordenado por precio.

    Por que existe (MEDIDO el 2026-08-25, ver el reporte del pozo):

    `buscar_ofertas` devuelve 64 ofertas como mucho -- la API corta ahi y pedir
    `limit` mayor no cambia nada -- y son las mas baratas. Con el filtro de
    familia de CPU que exige el proyecto (`--cpu E5-26`, que es lo que hace que
    el entrenamiento salga IDENTICO bit a bit entre maquinas) de esas 64 quedaban
    **19**. O sea que el pozo no lo fijaba el mercado sino el tope de la
    peticion: habia **156** maquinas E5-26xx alquilables y se veian 19. El 88 %
    del catalogo era invisible, y eso se pagaba dos veces -- un estudio no podia
    repartirse en mas de ~19 maquinas, y encima competia por ese puñado con
    cualquier otro estudio que corriera a la vez.

    POR QUE SE PAGINA POR vCPU Y NO POR PRECIO, que es lo obvio
    -----------------------------------------------------------
    La API no tiene `offset`, asi que hay que partir el espacio con un filtro. El
    precio parece el candidato natural (el orden ya es por precio), pero **el
    filtro de precio de Vast no es de fiar**. Medido el 2026-08-25 pidiendo
    `dph_total: {lte: X}` con todo lo demas igual:

        lte=0.06 -> 24 ofertas, la mas cara 0,0630   (se pasa del tope)
        lte=0.08 -> 64 ofertas, la mas cara 0,0813   (se pasa del tope)
        lte=0.12 -> 50 ofertas, la mas cara 0,1184
        lte=0.15 -> 61 ofertas, la mas cara 0,1511   (se pasa del tope)

    Ni respeta el tope ni el numero crece con el: con `lte=0.12` devuelve MENOS
    que con `lte=0.10`. La explicacion que encaja es que el servidor filtra por
    un precio distinto del `dph_total` que devuelve (el nuestro incluye el disco
    que pedimos). Sea cual sea la causa, **paginar por precio termina antes de
    tiempo**: una pagina corta se lee como "fin del catalogo" y no lo es. Asi es
    justamente como la primera version de esto daba 18 maquinas para `<=0,12` y
    57 para `<=0,10` -- menos pozo por subir el tope.

    `cpu_cores_effective` si parte exacto: los tramos [8,9), [9,10)... no se
    solapan ni dejan hueco, y sobre ellos el resultado si es monotono con el
    precio (50 / 85 / 99 / 125 / 142 maquinas para 0,08 / 0,10 / 0,12 / 0,15 /
    0,20 $/h). Por eso el recorrido va por ahi.

    ⚠ EL TOPE DE PRECIO SE APLICA AQUI, no en la API, y por lo de arriba: pasarle
    un `lte` que no respeta haria perder ofertas buenas en silencio.

    ⚠ Un tramo puede saturar igualmente (medido: [8,9) devolvio 64). Ese se
    sub-recorre subiendo el SUELO de precio, que es el unico filtro de precio que
    si se comporto en la medida (`gte` nunca devolvio nada por debajo). Si aun
    asi no avanza, se dice en voz alta en vez de callarse un hueco.
    """
    lo = int(cpus) if cpus else 1
    hi = int(max_cpus) if max_cpus else 256
    clave = (lo, hi, float(min_ram_gb), float(max_price))
    if usar_cache:
        guardado = _cache_catalogo.get(clave)
        if guardado and (time.time() - guardado[0]) < CACHE_TTL_S:
            return list(guardado[1])

    vistas: dict = {}

    def recoger(offs) -> None:
        for o in offs:
            if o.get("id") is not None:
                vistas.setdefault(str(o["id"]), o)

    for c in range(lo, hi, PASO_VCPU):
        tramo = buscar_ofertas(cpus=c, max_cpus=min(c + PASO_VCPU, hi),
                               min_ram_gb=min_ram_gb)
        recoger(tramo)
        if len(tramo) < TOPE_API:
            continue
        # tramo saturado: sub-recorrido subiendo el suelo de precio
        suelo = max(float(o.get("dph_total") or 0.0) for o in tramo)
        for _ in range(TOPE_API):
            sub = buscar_ofertas(cpus=c, max_cpus=min(c + PASO_VCPU, hi),
                                 min_ram_gb=min_ram_gb, min_price=suelo)
            recoger(sub)
            if len(sub) < TOPE_API:
                break
            techo = max(float(o.get("dph_total") or 0.0) for o in sub)
            if techo <= suelo:
                log(f"  ⚠ el tramo de {c} vCPU tiene {TOPE_API}+ ofertas al mismo "
                    f"precio ({techo:.4f} $/h): puede quedarse alguna sin ver")
                break
            suelo = techo

    fuera = [o for o in vistas.values()
             if not max_price or float(o.get("dph_total") or 0.0) <= max_price]
    fuera.sort(key=lambda o: float(o.get("dph_total") or 0.0))
    if usar_cache:
        _cache_catalogo[clave] = (time.time(), list(fuera))
    return fuera


def _leer_bloqueadas() -> dict:
    if not BLOQUEADAS.exists():
        return {"format_version": 1, "maquinas": []}
    try:
        datos = json.loads(BLOQUEADAS.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        # Ruidoso a proposito: un registro ilegible que se ignorara en silencio
        # volveria a mandar trabajo a las maquinas que ya se sabe que fallan, y
        # el sintoma seria "el estudio falla a veces", que no lleva hasta aqui.
        die(f"{BLOQUEADAS} no es JSON valido: {exc}\n"
            "  Arreglalo o borralo: es el registro de maquinas que fallaron.")
    if not isinstance(datos.get("maquinas"), list):
        die(f"{BLOQUEADAS} no tiene una lista 'maquinas'.")
    return datos


def _dias_desde(iso: str) -> float:
    try:
        cuando = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return 0.0          # fecha ilegible -> se trata como recien puesto
    return (datetime.now(timezone.utc) - cuando).total_seconds() / 86400


def maquinas_bloqueadas(incluir_caducadas: bool = False) -> dict:
    """{machine_id: registro} de las que NO se pueden volver a elegir.

    Caducadas fuera por defecto (BLOQUEO_DIAS). `incluir_caducadas` es para
    ensenar el registro entero, que es otra pregunta.
    """
    fuera = {}
    for m in _leer_bloqueadas()["maquinas"]:
        mid = m.get("machine_id")
        if mid is None:
            continue
        if incluir_caducadas or _dias_desde(m.get("ultimo_fallo", "")) < BLOQUEO_DIAS:
            fuera[int(mid)] = m
    return fuera


def bloquear_maquina(machine_id: int, motivo: str, etiqueta: str = "") -> dict:
    """Apunta (o renueva) una maquina que fallo. Devuelve su registro.

    Renovar en vez de duplicar: un host que falla tres veces es un dato mejor
    que tres lineas iguales, y `fallos` es lo que deja verlo.
    """
    machine_id = int(machine_id)
    datos = _leer_bloqueadas()
    ahora = ahora_iso()
    for m in datos["maquinas"]:
        if int(m.get("machine_id", -1)) == machine_id:
            m["fallos"] = int(m.get("fallos", 1)) + 1
            m["ultimo_fallo"] = ahora
            motivos = m.setdefault("motivos", [])
            if motivo and motivo not in motivos:
                motivos.append(motivo)
            registro = m
            break
    else:
        registro = {
            "machine_id": machine_id,
            "primer_fallo": ahora,
            "ultimo_fallo": ahora,
            "fallos": 1,
            "motivos": [motivo] if motivo else [],
            "visto_en": etiqueta,
        }
        datos["maquinas"].append(registro)
    datos["maquinas"].sort(key=lambda m: int(m.get("machine_id", 0)))
    tmp = BLOQUEADAS.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(datos, indent=2, ensure_ascii=False) + "\n",
                   encoding="utf-8")
    tmp.replace(BLOQUEADAS)
    return registro


def elegir_ofertas_distintas(cuantas: int, cpus: int, max_cpus: int,
                             min_ram_gb: float, max_price: float,
                             excluir_ofertas: set | None = None,
                             cpu: str = "") -> list:
    """`cuantas` ofertas, cada una en una MAQUINA FISICA distinta.

    Dos filtros que no son un lujo:

    - **Una por `machine_id`.** El catalogo publica varias ofertas por maquina
      (una por GPU libre), y coger "las N mas baratas" mete varias replicas en
      el mismo host: comparten CPU, disco y suerte, asi que si ese host va lento
      o se cae, se lleva por delante varias a la vez y encima las medidas dejan
      de ser independientes.
    - **Ninguna bloqueada** (BLOQUEADAS): el barrido del 2026-08-21 cayo dos
      veces en la misma oferta rota porque la eleccion coge siempre la mas
      barata y nada recordaba el fallo anterior.

    Ordena por precio, asi que coger una maquina distinta cuesta lo que cueste
    la siguiente oferta: es deliberado -- se paga por independencia.

    `cpu` filtra por nombre de procesador (subcadena, sin mayusculas), y no es
    un capricho de rendimiento: MEDIDO el 2026-08-23 corriendo el mismo estudio
    dos veces, el entrenamiento sale IDENTICO bit a bit -- mismo f1 al cuarto
    decimal y mismo numero de epocas -- entre maquinas de la familia Xeon E5-26xx
    v3/v4, y DIVERGE al cruzar a Xeon Silver (AVX-512), AMD EPYC o Core i7. La
    diferencia llego a 0,0457 en f1, mas que el efecto que aquel estudio media.
    Lo que manda no es la microarquitectura sino el juego de instrucciones
    vectoriales: un E5-2673 v3 (Haswell) y un E5-2680 v4 (Broadwell) dieron el
    mismo numero.

    O sea que fijar la familia convierte el ruido de maquina en CERO, y eso es
    lo que permite repartir un estudio entre muchas maquinas sin pagarlo en
    precision. El detalle esta en
    foveal-vision/docs/plan-lr-alto.md §7.4.

    ⚠ OJO CON LA SUBCADENA: lo medido son v3 y v4. "E5-26" deja pasar tambien
    las v2 (Ivy Bridge), que NO tienen AVX2 y por el propio razonamiento de
    arriba deberian divergir -- pero eso no se ha comprobado. Si hace falta la
    garantia, estrecha el filtro ("E5-2680 v4"); costo medido de estrechar a
    "E5-26": +2 % en el precio medio.
    """
    excluir_ofertas = excluir_ofertas or set()
    aguja = cpu.strip().lower()
    bloqueadas = maquinas_bloqueadas()
    # PAGINADO, y no las 64 de una peticion: con `cpu` puesto, el filtro de
    # familia se come la mayor parte de una pagina, asi que mirar solo las 64 mas
    # baratas dejaba el pozo en 19 maquinas cuando habia 143 (medido 2026-08-25,
    # ver buscar_ofertas_paginado). El pozo lo tiene que fijar el mercado y el
    # `--max-price`, no el tope de la peticion.
    ofertas = buscar_ofertas_paginado(cpus=cpus, max_cpus=max_cpus,
                                      min_ram_gb=min_ram_gb, max_price=max_price)
    elegidas, vistas, saltadas_bloq, saltadas_cpu = [], set(), 0, 0
    for o in ofertas:
        mid = o.get("machine_id")
        if mid is None or str(o.get("id")) in excluir_ofertas:
            continue
        if aguja and aguja not in (o.get("cpu_name") or "").lower():
            saltadas_cpu += 1
            continue
        if int(mid) in bloqueadas:
            saltadas_bloq += 1
            continue
        if int(mid) in vistas:
            continue
        vistas.add(int(mid))
        elegidas.append(o)
        if len(elegidas) == cuantas:
            break
    if saltadas_bloq:
        log(f"  ({saltadas_bloq} ofertas saltadas por estar su maquina bloqueada)")
    if saltadas_cpu:
        log(f"  ({saltadas_cpu} ofertas saltadas por no ser CPU '{cpu}')")
    if len(elegidas) < cuantas:
        die(
            f"Solo hay {len(elegidas)} maquinas DISTINTAS que cumplan "
            f">= {cpus} vCPU"
            + (f" y < {max_cpus}" if max_cpus else "")
            + (f", >= {min_ram_gb:g} GB RAM" if min_ram_gb else "")
            + (f", CPU '{cpu}'" if cpu else "")
            + f" por debajo de {max_price:.2f} $/h, y hacen falta {cuantas}.\n"
            "  Sube --max-price, afloja --cpus/--min-ram, o mira que hay:\n"
            f"    python3 scripts/vast_instance.py offers --cpus {cpus}\n"
            "  Las bloqueadas:  python3 scripts/vast_instance.py bloqueadas"
        )
    return elegidas


def cmd_bloqueadas(args: argparse.Namespace) -> None:
    todas = maquinas_bloqueadas(incluir_caducadas=True)
    vivas = maquinas_bloqueadas()
    if not todas:
        log("Ninguna maquina bloqueada. El registro esta en "
            f"{BLOQUEADAS.name} y se commitea.")
        return
    log(f"{'MAQUINA':>10}  {'FALLOS':>6}  {'ULTIMO FALLO':<20}  {'ESTADO':<10}  MOTIVOS")
    for mid, m in sorted(todas.items()):
        dias = _dias_desde(m.get("ultimo_fallo", ""))
        estado = "activo" if mid in vivas else f"caducado"
        log(f"{mid:>10}  {m.get('fallos', 1):>6}  "
            f"{m.get('ultimo_fallo', '?')[:19]:<20}  {estado:<10}  "
            f"{'; '.join(m.get('motivos', []))[:60]}")
    log(f"\nUn bloqueo se honra {BLOQUEO_DIAS} dias desde el ultimo fallo "
        f"({len(vivas)} activos de {len(todas)}).")
    log(f"Registro: {BLOQUEADAS}  (es dato commiteado: la maquina de control es efimera)")


def cmd_bloquear(args: argparse.Namespace) -> None:
    r = bloquear_maquina(args.machine_id, args.motivo, args.etiqueta)
    log(f"Maquina {r['machine_id']} bloqueada ({r['fallos']} fallo(s)). "
        f"No se volvera a elegir durante {BLOQUEO_DIAS} dias.")
    log(f"Acuerdate de commitear {BLOQUEADAS.name}: esta maquina no es persistente.")


def cmd_desbloquear(args: argparse.Namespace) -> None:
    datos = _leer_bloqueadas()
    antes = len(datos["maquinas"])
    datos["maquinas"] = [m for m in datos["maquinas"]
                         if int(m.get("machine_id", -1)) != int(args.machine_id)]
    if len(datos["maquinas"]) == antes:
        log(f"La maquina {args.machine_id} no estaba bloqueada.")
        return
    tmp = BLOQUEADAS.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(datos, indent=2, ensure_ascii=False) + "\n",
                   encoding="utf-8")
    tmp.replace(BLOQUEADAS)
    log(f"Maquina {args.machine_id} desbloqueada. Commitea {BLOQUEADAS.name}.")


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


# --- perfiles de busqueda: las condiciones de eleccion, como DATO ------------
#
# En Vast no se pide una maquina por su nombre: se BUSCA con un rango de vCPU,
# un minimo de RAM y un tope de precio. Esas condiciones se aprenden pagando
# -el barrido del 2026-08-21 cayo dos veces en la misma oferta rota porque
# `sweep` coge siempre la mas barata del rango, y se salio pidiendo --min-ram 8-
# asi que tienen que quedar escritas donde se puedan volver a usar, no en el
# README ni en la memoria de nadie.
#
# Es dato, no codigo, igual que benchmarks/, types/ y datasets/: anadir un
# perfil es anadir un fichero.

PERFIL_CAMPOS = ("cpus", "max_cpus", "min_ram", "max_price", "bench", "horas_max",
                 "disk", "image")


def load_perfil(name: str) -> dict:
    path = PERFIL_DIR / f"{name}.json"
    if not path.exists():
        disponibles = ", ".join(sorted(p.stem for p in PERFIL_DIR.glob("*.json")))
        die(
            f"No existe el perfil '{name}' (falta {path}).\n"
            f"  Definidos: {disponibles or 'ninguno'}\n"
            "  Miralos con: python3 scripts/vast_instance.py perfiles"
        )
    try:
        perfil = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        die(f"{path} no es JSON valido: {exc}")
    perfil["name"] = name
    return perfil


def all_perfiles() -> list[dict]:
    if not PERFIL_DIR.exists():
        return []
    return [load_perfil(p.stem) for p in sorted(PERFIL_DIR.glob("*.json"))]


def aplicar_perfil(args: argparse.Namespace) -> None:
    """Rellena lo que no venga en la linea de comandos con lo del perfil.

    Manda lo mas explicito, como en do_droplet.py: una opcion escrita pisa al
    perfil, y el perfil pisa al default. Por eso los campos que un perfil puede
    fijar tienen `default=None` en el parser: es la unica forma de distinguir
    "no lo pidio" de "lo pidio igual que el default".
    """
    nombre = getattr(args, "perfil", None)
    if not nombre:
        return
    perfil = load_perfil(nombre)
    puestos = []
    for campo in PERFIL_CAMPOS:
        if not hasattr(args, campo) or campo not in perfil:
            continue
        if getattr(args, campo) is None:
            setattr(args, campo, perfil[campo])
            puestos.append(f"{campo}={perfil[campo]}")
    log(f"Perfil '{nombre}': {', '.join(puestos) or 'nada que aplicar'}")
    if perfil.get("notas"):
        log(f"  Ojo: {perfil['notas']}")


def cmd_perfiles(args: argparse.Namespace) -> None:
    perfiles = all_perfiles()
    if not perfiles:
        log(
            "No hay perfiles.\n"
            "  Se definen en vast-perfiles/<nombre>.json y guardan las condiciones\n"
            "  de busqueda (cpus, min_ram, max_price, bench) para no repetirlas."
        )
        return
    for perfil in perfiles:
        log(f"{perfil['name']}")
        if perfil.get("descripcion"):
            log(f"  {perfil['descripcion']}")
        campos = ", ".join(
            f"{c}={perfil[c]}" for c in PERFIL_CAMPOS if c in perfil
        )
        log(f"  {campos or '(sin condiciones)'}")
        if perfil.get("notas"):
            log(f"  Ojo: {perfil['notas']}")
        log("")


def cmd_offers(args: argparse.Namespace) -> None:
    aplicar_perfil(args)
    tope = args.max_price or limite_precio()
    paginado = getattr(args, "paginado", False)
    if paginado:
        ofertas = buscar_ofertas_paginado(
            cpus=args.cpus, max_cpus=args.max_cpus,
            min_ram_gb=args.min_ram, max_price=tope,
        )
        if args.by_cpus:
            ofertas.sort(key=lambda o: -float(o.get("cpu_cores_effective") or 0))
    else:
        ofertas = buscar_ofertas(
            cpus=args.cpus,
            max_cpus=args.max_cpus,
            min_ram_gb=args.min_ram,
            max_price=tope,
            orden="cpu_cores_effective" if args.by_cpus else "dph_total",
        )
    aguja = (getattr(args, "cpu", "") or "").strip().lower()
    if aguja:
        ofertas = [o for o in ofertas
                   if aguja in (o.get("cpu_name") or "").lower()]
    if not ofertas:
        log(
            f"Ninguna oferta con {args.cpus or 'cualquier'} vCPU por debajo de "
            f"{tope:.2f} $/h" + (f" y CPU '{args.cpu}'" if aguja else "") + ".\n"
            "  Prueba a bajar --cpus, subir --max-price o quitar --min-ram."
        )
        return
    log(cabecera_ofertas())
    for o in ofertas[: args.limit]:
        log(oferta_fila(o))
    # El POZO es lo que de verdad se puede alquilar a la vez: una maquina FISICA
    # puede publicar varias ofertas (una por GPU libre) y el reparto coge una por
    # `machine_id`, asi que contar ofertas engaña. Y las bloqueadas no cuentan.
    bloqueadas = maquinas_bloqueadas()
    pozo = {int(o["machine_id"]) for o in ofertas
            if o.get("machine_id") is not None
            and int(o["machine_id"]) not in bloqueadas}
    log(f"\n{len(ofertas)} ofertas -> POZO de {len(pozo)} maquinas fisicas "
        f"distintas y no bloqueadas"
        + (f", CPU '{args.cpu}'" if aguja else "")
        + f", hasta {tope:.2f} $/h.")
    if not paginado:
        log("  ⚠ SIN --paginado solo se ven las 64 mas baratas (tope de la API). "
            "Con el filtro de CPU eso deja fuera la mayor parte del catalogo: "
            "medido el 2026-08-25, 19 visibles contra 143 reales.")
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
    aplicar_perfil(args)
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


def registro_datasets():
    """El modulo `dataset.py`, importado solo si algun benchmark pide datasets.

    Esto SI se importa, a diferencia de do_droplet.py, y la diferencia importa:
    `dataset.py` no es codigo de otro proveedor, es el registro de datos que
    comparten todos. Viaja siempre con este fichero en el mismo repo, asi que no
    rompe la regla de "cada script corre suelto"; lo que romperia esa regla es
    depender del lanzador de DigitalOcean para hablar con Vast.ai.
    """
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import dataset  # noqa: E402  -- import tardio a proposito

    return dataset


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


def datasets_del_bench(bench: dict) -> list[tuple[str, Path, str]]:
    """(nombre, tar.gz local ya verificado, destino relativo) de cada dataset.

    Se resuelven ANTES de alquilar nada. Si falta el dato, el barrido tiene que
    morir gratis: descubrirlo con la maquina ya encendida cuesta dinero y ademas
    deja la instancia viva mientras se depura.
    """
    nombres = bench.get("datasets") or []
    if not nombres:
        return []
    ds_mod = registro_datasets()
    salida = []
    for nombre in nombres:
        ds = ds_mod.load_dataset(nombre)
        log(f"  dataset '{nombre}':")
        blob = ds_mod.resolver_blob(ds)
        ds_mod.verificar(ds, blob)
        salida.append((nombre, blob, ds["destino"]))
    return salida


def construir_tar(envios: list[dict], datasets: list[tuple[str, Path, str]]) -> Path:
    """Empaqueta lo que hay que subir a la maquina alquilada.

    Se construye con `tarfile` en vez de llamar a `tar` para que esto funcione
    igual desde Windows, donde el barrido se depura antes de llevarlo al droplet.

    Va por SSH y no por `git clone` a proposito: clonar exigiria darle a un
    ordenador de un desconocido un token de GitHub. Los datasets viajan como su
    tar.gz tal cual, sin volver a comprimirlos: ya vienen empaquetados y
    verificados por `dataset.py`, y recomprimir solo gastaria tiempo.
    """
    tmp = Path(tempfile.mkdtemp(prefix="vast-bench-")) / "payload.tar.gz"
    with tarfile.open(tmp, "w:gz") as tar:
        for nombre, blob, _destino in datasets:
            tar.add(str(blob), arcname=f"_datasets/{nombre}.tar.gz")
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


def subir_payload(
    host: str,
    port: int,
    tar_path: Path,
    datasets: list[tuple[str, Path, str]],
    timeout: int,
) -> None:
    with tar_path.open("rb") as fh:
        proc = subprocess.run(
            ssh_command(host, port) + ["cat > /root/payload.tar.gz"],
            stdin=fh,
            timeout=timeout,
        )
    if proc.returncode != 0:
        raise RuntimeError("no pude subir el payload a la instancia")

    lineas = [
        "set -eu",
        "mkdir -p /root/bench",
        "tar -xzf /root/payload.tar.gz -C /root/bench",
    ]
    for nombre, _blob, destino in datasets:
        # El destino viene del descriptor del dataset y es relativo a la raiz de
        # trabajo, que en la instancia es /root/bench: asi el repo y su dato
        # quedan colocados como los espera el proyecto, sin que el benchmark
        # tenga que saber que esta en una maquina alquilada.
        lineas += [
            f'D="/root/bench/{destino}"',
            'mkdir -p "$D"',
            f'tar -xzf "/root/bench/_datasets/{nombre}.tar.gz" -C "$D"',
            f'echo "  dataset {nombre}: $(find "$D" -type f | wc -l) ficheros"',
        ]
    lineas.append('rm -rf /root/bench/_datasets /root/payload.tar.gz')
    if ssh_script(host, port, "\n".join(lineas) + "\n", timeout) != 0:
        raise RuntimeError("el payload subio pero no se pudo desempaquetar")


def correr_bench(
    host: str, port: int, bench: dict, timeout: int
) -> tuple[dict, dict]:
    """Instala, mide, y devuelve el reporte junto con los tiempos de cada tramo.

    `install` y `run` van en dos pasos para que el log diga cual de los dos
    fallo: instalar torch tarda minutos y falla por motivos (red, disco) que no
    tienen nada que ver con los del benchmark en si.

    Los dos tiempos se guardan porque miden cosas distintas y las dos importan
    para decidir donde entrenar: `medida` es lo rapida que es la maquina, e
    `instalacion` es el peaje que hay que pagar ANTES de la primera epoca. Una
    maquina el doble de rapida que tarda diez minutos en estar lista sale peor
    para un entrenamiento corto, y sin este numero eso no se ve.
    """
    prologo = "set -eu\ncd /root/bench\n"

    log("  instalando dependencias...")
    inicio = time.time()
    if ssh_script(host, port, prologo + bench["install"] + "\n", timeout) != 0:
        raise RuntimeError("fallo la instalacion de dependencias en la instancia")
    instalacion = time.time() - inicio
    log(f"  instalado en {instalacion:.0f}s. Midiendo...")

    inicio = time.time()
    if ssh_script(host, port, prologo + bench["run"] + "\n", timeout) != 0:
        raise RuntimeError("el benchmark fallo al ejecutarse")
    medida = time.time() - inicio
    log(f"  medido en {medida:.0f}s. Recogiendo el reporte...")

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
    tiempos = {
        "instalacion_s": round(instalacion, 1),
        "medida_s": round(medida, 1),
    }
    return reporte, tiempos


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

    unidad = bench.get("unidad", "tiempo")
    ref = bench.get("referencia") or {}
    ref_valor = float(ref.get("valor") or 0)
    ref_precio = float(ref.get("usd_hora") or 0)

    filas = []
    for m in medidas:
        maq = m.get("maquina", {})
        t = m.get("tiempos") or {}
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
                # Lo que cuesta UNA unidad de trabajo, no una hora de maquina.
                # Es el numero que decide si la maquina cara sale a cuenta: una
                # que va el doble de rapida por el doble de precio empata aqui.
                "usd_unidad": (precio * valor / 3600)
                if isinstance(valor, (int, float)) and valor
                else None,
                "listo": t.get("listo_en_s"),
                "medido": m.get("medido", "")[:10],
            }
        )
    filas.sort(key=lambda f: (f["valor"] is None, f["valor"]))

    lineas = [
        f"# {bench['name']}: {bench.get('descripcion', '')}",
        "",
        "Generada por `vast_instance.py sweep`. **No se edita a mano**: se rehace",
        "entera a partir de los JSON de este directorio, que son el dato.",
        "",
        f"La columna que manda es **{unidad}**; la tabla va ordenada por ella, de",
        "mas rapida a mas lenta.",
        "",
        f"| {unidad} | x vs. base | listo en | $/h | $/unidad | vCPU | CPU | RAM GB | ubicacion | fecha |",
        "|---:|---:|---:|---:|---:|---:|---|---:|---|---|",
    ]
    for f in filas:
        valor = f"**{f['valor']:.2f}**" if isinstance(f["valor"], (int, float)) else "-"
        if ref_valor and isinstance(f["valor"], (int, float)) and f["valor"]:
            veces = f"{ref_valor / f['valor']:.2f}x"
        else:
            veces = "-"
        listo = f"{f['listo'] / 60:.1f} min" if f["listo"] else "-"
        usd_u = f"{f['usd_unidad']:.5f}" if f["usd_unidad"] is not None else "-"
        lineas.append(
            f"| {valor} | {veces} | {listo} | {f['usd_hora']:.4f} | {usd_u} | "
            f"{f['vcpu']:g} | {f['cpu'][:30]} | {f['ram']:.1f} | {f['sitio']} | "
            f"{f['medido']} |"
        )

    lineas += [
        "",
        "## Como se lee",
        "",
        f"- **{unidad}** es el numero que se quiere bajar.",
    ]
    if ref_valor:
        lineas.append(
            f"- **x vs. base** compara contra {ref.get('nombre', 'la referencia')}"
            f" ({ref_valor:g}). Un 2,00x es la mitad de tiempo."
        )
    lineas += [
        "- **listo en** es lo que se tarda desde que se alquila hasta la primera",
        "  unidad de trabajo: arrancar, subir el codigo e instalar. **Es un peaje",
        "  que hay que amortizar**: una maquina el doble de rapida que tarda 6",
        "  minutos en estar lista sale peor para un entrenamiento de tres epocas.",
        "- **$/unidad** es el coste del trabajo, no de la hora. Es lo que decide si",
        "  la maquina cara compensa: una que va el doble de rapida por el doble de",
        "  precio empata en esta columna, y entonces lo unico que compras es tiempo",
        "  de reloj.",
    ]
    if ref_valor and ref_precio:
        ref_unidad = ref_precio * ref_valor / 3600
        lineas.append(
            f"- La base ({ref.get('nombre', '?')}) sale a **{ref_unidad:.5f} $** por"
            f" unidad, a {ref_precio:.4f} $/h."
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


def medir_en_oferta(
    bench: dict,
    oferta: dict,
    tar_path: Path,
    datasets: list,
    etiqueta: str,
) -> dict:
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
        arranque = time.time() - arrancada
        log(f"  SSH listo en {host}:{port}. Subiendo {tar_path.stat().st_size / 1e6:.1f} MB...")

        inicio_subida = time.time()
        subir_payload(host, port, tar_path, datasets, timeout=600)
        subida = time.time() - inicio_subida

        reporte, tiempos = correr_bench(host, port, bench, timeout)
        tiempos["arranque_s"] = round(arranque, 1)
        tiempos["subida_s"] = round(subida, 1)
        # Lo que hay que esperar antes de la PRIMERA epoca. Es el peaje de usar
        # una maquina alquilada, y hay que amortizarlo: para tres epocas puede
        # pesar mas que la velocidad de la maquina.
        tiempos["listo_en_s"] = round(
            arranque + subida + tiempos["instalacion_s"], 1
        )

        vivida = time.time() - arrancada
        return {
            "format_version": 2,
            "benchmark": bench["name"],
            "proveedor": "vast.ai",
            "medido": ahora_iso(),
            "instancia": iid,
            "oferta": oferta.get("id"),
            "maquina": resumen_maquina(oferta),
            "usd_hora": round(precio, 5),
            "usd_medida": round(precio * vivida / 3600, 5),
            "segundos_vivida": round(vivida, 1),
            "tiempos": tiempos,
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
    aplicar_perfil(args)
    if not args.bench:
        die("Dime que medir: --bench <nombre>, o --perfil <nombre> si lo declara.")
    bench = load_bench(args.bench)
    tope = args.max_price or limite_precio()
    oferta = elegir_oferta(args.cpus, args.max_cpus, args.min_ram, tope)

    if args.dry_run:
        log(cabecera_ofertas())
        log(oferta_fila(oferta))
        log("\n--dry-run: no se alquila nada.")
        return

    datasets = datasets_del_bench(bench)
    tar_path = construir_tar(bench["envia"], datasets)
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
    aplicar_perfil(args)
    if not args.bench:
        die("Dime que medir: --bench <nombre>, o --perfil <nombre> si lo declara.")
    bench = load_bench(args.bench)
    tope = args.max_price or limite_precio()
    # Los defaults se resuelven AQUI y no en el parser: con `default=None` es
    # como se distingue "no lo pidio" (y manda el perfil) de "lo pidio igual
    # que el default" (y manda lo escrito).
    args.cpus = args.cpus or "2,4,8,16"
    args.horas_max = 1.0 if args.horas_max is None else args.horas_max
    try:
        niveles = [int(n) for n in str(args.cpus).split(",") if n.strip()]
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

    datasets = datasets_del_bench(bench)
    tar_path = construir_tar(bench["envia"], datasets)
    log(f"Payload listo: {tar_path.stat().st_size / 1e6:.1f} MB")

    hechos, fallados = [], []
    for n, oferta in elegidas:
        try:
            resultado = medir_en_oferta(
                bench, oferta, tar_path, datasets, f"sweep-{n}vcpu"
            )
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
    p.add_argument("--perfil", help="condiciones guardadas en vast-perfiles/ (lo explicito manda)")
    p.add_argument("--cpus", type=int, default=None, help="vCPU efectivas minimas")
    p.add_argument("--max-cpus", type=int, default=None, help="vCPU efectivas maximas")
    p.add_argument("--min-ram", type=float, default=None, metavar="GB")
    p.add_argument("--max-price", type=float, default=None, metavar="USD_HORA")
    p.add_argument("--limit", type=int, default=20, help="cuantas filas imprimir")
    p.add_argument("--cpu", default="",
                   help="solo esta CPU (subcadena, p.ej. 'E5-26'): es el filtro "
                        "que usa el reparto de estudios, asi que es el unico que "
                        "enseña el pozo de verdad")
    p.add_argument("--paginado", action="store_true",
                   help="recorre el catalogo ENTERO por tramos de precio en vez "
                        "de quedarse en las 64 mas baratas que da la API")
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
    p.add_argument("--perfil", help="condiciones guardadas en vast-perfiles/ (lo explicito manda)")
    p.add_argument("--cpus", type=int, default=None, help="vCPU efectivas minimas")
    p.add_argument("--max-cpus", type=int, default=None)
    p.add_argument("--min-ram", type=float, default=None, metavar="GB")
    p.add_argument("--offer", help="alquilar esta oferta concreta, sin buscar")
    p.add_argument("--image", help=f"imagen Docker (por defecto {DEFAULTS['VAST_IMAGE']})")
    p.add_argument("--disk", type=float, default=None, metavar="GB")
    p.add_argument("--max-price", type=float, default=None, metavar="USD_HORA")
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

    p = sub.add_parser(
        "perfiles",
        help="condiciones de busqueda guardadas en vast-perfiles/, para no repetirlas",
    )
    p.set_defaults(func=cmd_perfiles)

    p = sub.add_parser(
        "bloqueadas",
        help="maquinas que fallaron y no se vuelven a elegir (dato commiteado)",
    )
    p.set_defaults(func=cmd_bloqueadas)

    p = sub.add_parser("bloquear", help="apunta una maquina que fallo")
    p.add_argument("machine_id", type=int)
    p.add_argument("--motivo", default="", help="que le paso, en una linea")
    p.add_argument("--etiqueta", default="", help="donde se vio (etiqueta del trabajo)")
    p.set_defaults(func=cmd_bloquear)

    p = sub.add_parser("desbloquear", help="quita una maquina del registro")
    p.add_argument("machine_id", type=int)
    p.set_defaults(func=cmd_desbloquear)

    p = sub.add_parser("benchs", help="benchmarks disponibles en benchmarks/")
    p.set_defaults(func=cmd_benchs)

    p = sub.add_parser(
        "bench", help="alquila UNA maquina, corre el benchmark y la destruye"
    )
    p.add_argument("--perfil", help="condiciones guardadas en vast-perfiles/ (lo explicito manda)")
    p.add_argument("--bench", default=None, help="nombre de un fichero de benchmarks/")
    p.add_argument("--cpus", type=int, default=None)
    p.add_argument("--max-cpus", type=int, default=None)
    p.add_argument("--min-ram", type=float, default=None, metavar="GB")
    p.add_argument("--max-price", type=float, default=None, metavar="USD_HORA")
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(func=cmd_bench)

    p = sub.add_parser(
        "sweep", help="una maquina por nivel de CPU: mide, guarda y destruye"
    )
    p.add_argument("--perfil", help="condiciones guardadas en vast-perfiles/ (lo explicito manda)")
    p.add_argument("--bench", default=None)
    p.add_argument(
        "--cpus",
        default=None,
        help="niveles de vCPU separados por coma (por defecto 2,4,8,16)",
    )
    p.add_argument("--min-ram", type=float, default=None, metavar="GB")
    p.add_argument("--max-price", type=float, default=None, metavar="USD_HORA")
    p.add_argument(
        "--horas-max",
        type=float,
        default=None,
        help="solo para estimar el coste maximo antes de empezar (por defecto 1)",
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
