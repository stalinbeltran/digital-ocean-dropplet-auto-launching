#!/usr/bin/env python3
"""Que el descriptor de sispla-demo sea coherente consigo mismo y con el mini.

    python3 tests/test_sispla_demo.py

Sin framework y sin dependencias, como el resto de este repo.

QUE SE FIJA AQUI: que los tres campos DELEGAN en `herramientas/servicio.sh` del
repo de la app (R7: la logica vive en quien produce; el puerto se decide alli,
en un solo sitio), que no hay env_prefix (la app no lee .env), y que el
servicio esta en types/mini.json, que es lo que hace que «cada vez que lo
reinicie» sea verdad: un servicio que no esta en el tipo no nace con la maquina.

⚠ Lo que este test NO cubre: que servicio.sh haga lo que dice. Eso lo prueba
la app en `herramientas/probar-servicio.sh` (modo seco), y que `url` se niegue
con el puerto cerrado se midio a mano el 2026-09-15 en un dev sin el 8080
abierto: exit 1 y «ufw no deja pasar el 8080/tcp» por stderr.
"""

import importlib.util
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def cargar():
    spec = importlib.util.spec_from_file_location(
        "do_droplet", ROOT / "scripts" / "do_droplet.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def main() -> int:
    mod = cargar()
    svc = mod.load_service("sispla-demo")
    fallos = 0

    def caso(nombre: str, ok: bool, detalle: str = "") -> None:
        nonlocal fallos
        fallos += not ok
        print(f"  {'ok   ' if ok else 'FALLO'} {nombre}{'  -> ' + detalle if detalle else ''}")

    for campo, modo in (("install", "instalar"), ("start", "servir"), ("url", "url")):
        caso(f"{campo} delega en la app (R7): servicio.sh {modo}",
             svc[campo] == f"sh herramientas/servicio.sh {modo}", svc[campo])
    # La app lee process.env, no .env, y la unidad no lo sourcea: un env_prefix
    # escribiria un fichero que nadie lee junto al codigo que se sirve por HTTP.
    caso("sin env_prefix", not svc["env_prefix"], svc["env_prefix"])

    mini = json.loads((ROOT / "types" / "mini.json").read_text(encoding="utf-8"))
    caso("types/mini.json lo lleva", "sispla-demo" in mini.get("services", []))

    total = 5
    print(f"\n{total - fallos}/{total} pasan")
    return 1 if fallos else 0


if __name__ == "__main__":
    sys.exit(main())
