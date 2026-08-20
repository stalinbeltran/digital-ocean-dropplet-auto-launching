#!/usr/bin/env python3
"""Comprueba que el token de Vast.ai funciona, sin alquilar nada.

Es la primera pieza del trabajo de comparar GPUs entre proveedores (ver
gpu_training_services.md): antes de escribir un lanzador conviene saber que la
clave entra, que la cuenta puede pagar y que el catálogo responde.

**Este script no crea instancias y no gasta dinero.** Todas las llamadas son de
lectura salvo el catálogo, que es un POST porque la API de Vast.ai busca así
(POST /bundles/ es una consulta, no una creación). No hay ningún PUT a /asks/,
que es lo único que alquila una máquina.

    python scripts/vast_check.py
    python scripts/vast_check.py --json

Sale con código 0 si todo lo imprescindible pasa, y 1 si algo falla. Los avisos
(saldo a cero, sin claves SSH) no hacen fallar: el token es correcto, lo que
pasa es que aún no puedes alquilar.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
# Sin barra final: las rutas la llevan ya, tal cual salen del OpenAPI de Vast.ai
# (https://docs.vast.ai/api-reference/openapi.json).
API = "https://console.vast.ai"


# ---------------------------------------------------------------- configuración


def load_env() -> None:
    """Carga .env sin dependencias. Las variables reales del entorno mandan.

    Copiado a propósito de do_droplet.py en vez de importarlo: los dos scripts
    tienen que poder ejecutarse sueltos, sin que uno arrastre al otro.
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
            "  lanzar máquinas hace falta una con permiso de escritura sobre\n"
            "  instancias, no una de sólo lectura."
        )
    return tok


def die(msg: str) -> "NoReturn":  # type: ignore[valid-type]
    print(f"\nERROR: {msg}\n", file=sys.stderr)
    raise SystemExit(1)


def force_utf8_output() -> None:
    """La consola de Windows usa cp1252 por defecto y peta con acentos."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except (AttributeError, OSError):
            pass


# ------------------------------------------------------------------ cliente API


class ApiError(Exception):
    """Fallo de una llamada concreta. Se captura para que una prueba pueda
    marcarse como fallida sin llevarse por delante a las demás."""


def api(method: str, path: str, body: dict | None = None, auth: bool = True) -> dict | list:
    """Petición a la API de Vast.ai, con reintentos ante 429 y 5xx.

    Vast.ai limita a unas 3 peticiones por segundo POR ENDPOINT y contesta 429
    con `API requests too frequent endpoint threshold=3.0`. Sin el reintento,
    correr las pruebas dos veces seguidas da un falso fallo.
    """
    url = f"{API}{path}"
    payload = json.dumps(body).encode("utf-8") if body is not None else None

    last_error = ""
    for attempt in range(5):
        req = urllib.request.Request(url, data=payload, method=method)
        if auth:
            req.add_header("Authorization", f"Bearer {token()}")
        req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=45) as resp:
                raw = resp.read()
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:300]
            if exc.code == 429 and attempt < 4:
                time.sleep(1 + attempt)
                continue
            if exc.code >= 500 and attempt < 4:
                time.sleep(2**attempt)
                continue
            if exc.code in (401, 403):
                raise ApiError(
                    f"HTTP {exc.code}: el token no vale para {method} {path}. "
                    "O está mal copiado, o la clave se creó con permisos recortados. "
                    f"Respuesta: {detail}"
                )
            raise ApiError(f"HTTP {exc.code} en {method} {path}: {detail}")
        except (urllib.error.URLError, OSError) as exc:
            # urlopen() puede volver bien y expirar al leer el cuerpo; eso llega
            # como TimeoutError, que no es URLError. Misma lección que en
            # do_droplet.py, y por eso se capturan los dos.
            last_error = f"{type(exc).__name__}: {exc}"
            time.sleep(2**attempt)
    raise ApiError(f"Sin respuesta tras varios reintentos: {last_error}")


# ---------------------------------------------------------------------- pruebas
#
# Cada prueba devuelve (estado, resumen, datos):
#   "ok"     lo esperado
#   "aviso"  el token va bien pero falta algo para poder alquilar
#   "fallo"  algo que impide seguir
# El contrato es que ninguna lanza excepciones al llamador: `ejecutar` las
# envuelve para que un endpoint caído no oculte el resultado de los demás.


def prueba_identidad() -> tuple[str, str, dict]:
    """GET /users/current/ -- lo más barato que demuestra que la clave entra."""
    u = api("GET", "/api/v0/users/current/")
    assert isinstance(u, dict)
    datos = {
        "id": u.get("id"),
        "usuario": u.get("username"),
        "email_verificado": bool(u.get("email_verified")),
    }
    return "ok", f"autenticado como {datos['usuario']} (id {datos['id']})", datos


def prueba_saldo() -> tuple[str, str, dict]:
    """Saldo de la cuenta.

    Es aviso y no fallo a propósito: una clave recién creada en una cuenta sin
    tarjeta autentica perfectamente y falla sólo al alquilar. Mejor enterarse
    aquí que con un PUT a /asks/ que no se entiende por qué se rechaza.
    """
    u = api("GET", "/api/v0/users/current/")
    assert isinstance(u, dict)
    saldo = float(u.get("credit") or 0.0)
    datos = {
        "credito_usd": saldo,
        "tiene_facturacion": bool(u.get("has_billing")),
        "ha_alquilado_antes": bool(u.get("has_rented")),
    }
    if saldo <= 0:
        return (
            "aviso",
            "saldo 0,00 $: el token lee bien, pero no podrás alquilar hasta "
            "cargar credito en https://cloud.vast.ai/billing/",
            datos,
        )
    return "ok", f"credito {saldo:.2f} $", datos


def prueba_catalogo() -> tuple[str, str, dict]:
    """POST /bundles/ -- la búsqueda del marketplace.

    Es el endpoint del que cuelga todo lo demás: sin catálogo no hay nada que
    elegir. Se comprueba además que traiga `dlperf`, que es la métrica de
    rendimiento ya medida por Vast.ai y la razón de empezar por este proveedor.
    """
    resp = api("POST", "/api/v0/bundles/", {"limit": 64, "rentable": {"eq": True}})
    assert isinstance(resp, dict)
    ofertas = resp.get("offers") or []
    if not ofertas:
        return "fallo", "el catálogo respondió sin ninguna oferta", {"ofertas": 0}

    modelos = {o.get("gpu_name") for o in ofertas if o.get("gpu_name")}
    con_dlperf = sum(1 for o in ofertas if o.get("dlperf"))
    barata = min(ofertas, key=lambda o: o["dph_total"] / max(1, o.get("num_gpus") or 1))
    precio = barata["dph_total"] / max(1, barata.get("num_gpus") or 1)
    datos = {
        "ofertas": len(ofertas),
        "modelos_gpu": len(modelos),
        "ofertas_con_dlperf": con_dlperf,
        "mas_barata": {"gpu": barata.get("gpu_name"), "usd_gpu_hora": round(precio, 4)},
    }
    return (
        "ok",
        f"{len(ofertas)} ofertas, {len(modelos)} modelos de GPU; "
        f"la más barata es {barata.get('gpu_name')} a {precio:.3f} $/GPU-h",
        datos,
    )


def prueba_benchmarks() -> tuple[str, str, dict]:
    """GET /benchmarks/ -- las puntuaciones ya medidas por máquina.

    Esto es lo que hace a Vast.ai distinto para el objetivo de comparar
    velocidades: hay una línea base publicada contra la que contrastar el
    benchmark propio antes de gastar nada.

    OJO, el OpenAPI de Vast.ai MIENTE en este endpoint. Documenta los campos
    `score`, `model` y `name`; lo que llega de verdad (medido el 2026-08-20) es
    `value`, `gpu_name` y `type`, y los tres documentados vienen a null en las
    200 marcas devueltas. Leer `model` daba "0 modelos" sin que fallara nada,
    que es la peor clase de error: silencioso y creíble. Si algún día se toca
    esto, compruébalo contra la respuesta real, no contra la especificación.
    """
    resp = api("GET", "/api/v0/benchmarks/")
    marcas = resp.get("benchmarks") if isinstance(resp, dict) else resp
    marcas = marcas or []
    if not marcas:
        return "aviso", "el endpoint responde pero no devolvió ninguna marca", {"marcas": 0}
    gpus = {m.get("gpu_name") for m in marcas if m.get("gpu_name")}
    con_valor = sum(1 for m in marcas if m.get("value") is not None)
    datos = {"marcas": len(marcas), "modelos_gpu": len(gpus), "con_valor": con_valor}
    if not gpus:
        return (
            "aviso",
            f"{len(marcas)} marcas, pero ninguna trae gpu_name: la respuesta ha cambiado",
            datos,
        )
    return "ok", f"{len(marcas)} marcas sobre {len(gpus)} modelos de GPU", datos


def prueba_instancias() -> tuple[str, str, dict]:
    """GET /instances/ -- qué hay vivo AHORA MISMO.

    Doble función: confirma el permiso de lectura sobre instancias, y avisa si
    quedó algo encendido facturando. Es el equivalente al `list` de
    do_droplet.py, y responde al objetivo 2 del proyecto.
    """
    resp = api("GET", "/api/v1/instances/")
    assert isinstance(resp, dict)
    vivas = resp.get("instances") or []
    coste = sum(float(i.get("dph_total") or 0) for i in vivas)
    datos = {"instancias": len(vivas), "usd_hora": round(coste, 4)}
    if vivas:
        return (
            "aviso",
            f"{len(vivas)} instancia(s) encendida(s), {coste:.3f} $/h en total",
            datos,
        )
    return "ok", "ninguna instancia encendida (no se está facturando nada)", datos


def prueba_claves_ssh() -> tuple[str, str, dict]:
    """GET /ssh/ -- las claves con las que se entrará a la máquina alquilada.

    Aviso y no fallo: es un requisito para el siguiente paso, no para el token.
    Un droplet al que no se entra es un droplet muerto (objetivo 6), y en
    Vast.ai la clave hay que registrarla antes de crear la instancia.
    """
    resp = api("GET", "/api/v0/ssh/")
    claves = resp.get("ssh_keys") if isinstance(resp, dict) else resp
    claves = claves or []
    datos = {"claves": len(claves)}
    if not claves:
        return (
            "aviso",
            "no hay ninguna clave SSH registrada; hará falta una antes de "
            "alquilar (POST /api/v0/ssh/ o https://cloud.vast.ai/manage-keys/)",
            datos,
        )
    return "ok", f"{len(claves)} clave(s) SSH registrada(s)", datos


def prueba_catalogo_sin_clave() -> tuple[str, str, dict]:
    """El catálogo contestando SIN token.

    Está aquí porque es la afirmación en la que se apoya la recomendación de
    gpu_training_services.md ("se puede explorar el mercado sin cuenta"), y las
    afirmaciones que se escriben se comprueban. Si algún día Vast.ai cierra el
    endpoint, esta prueba es la que avisa.
    """
    resp = api("POST", "/api/v0/bundles/", {"limit": 4}, auth=False)
    assert isinstance(resp, dict)
    ofertas = resp.get("offers") or []
    datos = {"ofertas": len(ofertas)}
    if not ofertas:
        return "aviso", "el catálogo ya no responde sin autenticar", datos
    return "ok", f"responde sin token ({len(ofertas)} ofertas)", datos


PRUEBAS = [
    ("identidad", prueba_identidad),
    ("saldo", prueba_saldo),
    ("catalogo", prueba_catalogo),
    ("benchmarks", prueba_benchmarks),
    ("instancias", prueba_instancias),
    ("claves-ssh", prueba_claves_ssh),
    ("catalogo-sin-clave", prueba_catalogo_sin_clave),
]

MARCA = {"ok": "OK   ", "aviso": "AVISO", "fallo": "FALLO"}


def ejecutar() -> list[dict]:
    resultados = []
    for nombre, funcion in PRUEBAS:
        try:
            estado, resumen, datos = funcion()
        except ApiError as exc:
            estado, resumen, datos = "fallo", str(exc), {}
        resultados.append(
            {"prueba": nombre, "estado": estado, "resumen": resumen, "datos": datos}
        )
    return resultados


def main() -> int:
    force_utf8_output()
    parser = argparse.ArgumentParser(
        description="Comprueba el token de Vast.ai sin alquilar nada.",
        epilog="No crea instancias: todas las llamadas son de lectura o de búsqueda.",
    )
    parser.add_argument(
        "--json", action="store_true", help="salida en JSON, para encadenar con otra cosa"
    )
    args = parser.parse_args()

    load_env()
    token()  # falla pronto y con instrucciones si no está puesto

    resultados = ejecutar()

    if args.json:
        print(json.dumps(resultados, indent=2, ensure_ascii=False))
    else:
        print("\nComprobando el token de Vast.ai (sin alquilar nada)\n")
        for r in resultados:
            print(f"  [{MARCA[r['estado']]}] {r['prueba']:20} {r['resumen']}")
        fallos = sum(1 for r in resultados if r["estado"] == "fallo")
        avisos = sum(1 for r in resultados if r["estado"] == "aviso")
        print()
        if fallos:
            print(f"  {fallos} prueba(s) fallida(s). El token no sirve todavía.")
        elif avisos:
            print(f"  El token funciona. {avisos} aviso(s): lee arriba antes de lanzar.")
        else:
            print("  El token funciona y la cuenta está lista para alquilar.")
        print()

    return 1 if any(r["estado"] == "fallo" for r in resultados) else 0


if __name__ == "__main__":
    raise SystemExit(main())
