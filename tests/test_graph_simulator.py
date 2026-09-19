#!/usr/bin/env python3
"""Que el descriptor de graph-simulator sea coherente consigo mismo, con el mini y con la app.

    python3 tests/test_graph_simulator.py

Sin framework y sin dependencias, como el resto de este repo.

QUE SE FIJA AQUI, y por que cada cosa:

1. **Los tres campos DELEGAN en `herramientas/servicio.sh`** del repo de la app (R7: la
   logica vive en quien produce). El lanzador solo nombra; instalar, servir y anunciar
   se deciden alli.

2. **El PUERTO no aparece en el descriptor.** Vive en `PORT:-8040` de servicio.sh y en
   ningun sitio mas. Escribirlo aqui crea un segundo sitio que puede DERIVAR del
   primero: la unidad arranca en un puerto y `url` anuncia otro, el mismo par «se
   vigilaba una mitad y fallo la otra» que costo la web movil cuatro veces.

3. **Sin env_prefix**: la app no lee .env ni tiene nada que configurar en el server;
   declararlo escribiria un fichero que nadie lee.

4. **Que el mini lo lleve.** Un servicio que no esta en el tipo no nace con la maquina,
   y entonces «esta instalado» dura hasta el siguiente `launch mini`.

5. **Que la app diga lo mismo que el descriptor**: solo comprobable con el repo al lado.
   Si esta, se corre su propio `probar-servicio.sh` (modo seco: no toca ufw ni arranca
   nada) y se lee el puerto por defecto de servicio.sh; si no esta, SE DICE en vez de
   callarse (el `NO SE` de siempre).

⚠ Lo que este test NO cubre: la instalacion de verdad. Eso se mide contra el mini vivo
(unidad `active`, `NRestarts=0`, y un `curl` DESDE OTRA MAQUINA, que es lo unico que
prueba la red de en medio), y se anota en el commit que la hizo.
"""

import importlib.util
import json
import re
import subprocess
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
    svc = mod.load_service("graph-simulator")
    fallos = 0
    total = 0

    def caso(nombre: str, ok: bool, detalle: str = "") -> None:
        nonlocal fallos, total
        total += 1
        fallos += not ok
        print(f"  {'ok   ' if ok else 'FALLO'} {nombre}{'  -> ' + detalle if detalle else ''}")

    caso("el repo es graph-simulator y se clona en ~/src/graph-simulator",
         svc["repo"] == "stalinbeltran/graph-simulator" and svc["dir"] == "graph-simulator",
         f'{svc["repo"]} -> {svc["dir"]}')

    # (1) Delegacion, campo a campo.
    for campo, modo in (("install", "instalar"), ("start", "servir"), ("url", "url")):
        caso(f"{campo} delega en la app (R7): servicio.sh {modo}",
             svc[campo] == f"sh herramientas/servicio.sh {modo}", svc[campo])

    # (2) El puerto vive en UN sitio. Cualquier numero de 4 digitos aqui es deriva.
    for campo in ("install", "start", "url"):
        puertos = re.findall(r"\b\d{4}\b", svc[campo])
        caso(f"{campo} NO declara puerto (vive en PORT de servicio.sh)",
             not puertos, ", ".join(puertos))

    # (3) Nada que configurar.
    caso("sin env_prefix", not svc["env_prefix"], svc["env_prefix"])
    caso("sin pre_destroy (no produce nada que recoger)", not svc["pre_destroy"])

    # (4) Un servicio que no esta en el tipo no nace con la maquina.
    mini = json.loads((ROOT / "types" / "mini.json").read_text(encoding="utf-8"))
    caso("types/mini.json lo lleva", "graph-simulator" in mini.get("services", []))

    # (5) La app, si esta al lado.
    hermano = ROOT.parent / "graph-simulator"
    guion = hermano / "herramientas" / "servicio.sh"
    if guion.is_file():
        texto = guion.read_text(encoding="utf-8")
        caso("servicio.sh decide el puerto en un solo sitio (PORT:-8040)",
             'PUERTO="${PORT:-8040}"' in texto,
             "si cambia, cambia la URL y el `ufw allow`: no hay otro sitio que actualizar")
        r = subprocess.run(["sh", str(hermano / "herramientas" / "probar-servicio.sh")],
                           capture_output=True, text=True, timeout=60)
        ultima = (r.stdout.strip().splitlines() or ["sin salida"])[-1]
        caso("probar-servicio.sh de la app pasa (modo seco)", r.returncode == 0, ultima)
    else:
        print(f"  NO SE  {hermano} no esta clonado: no puedo comprobar el puerto ni correr "
              f"su probar-servicio.sh. No cuenta como fallo, pero tampoco como comprobado.")

    print(f"\n{total - fallos}/{total} pasan")
    return 1 if fallos else 0


if __name__ == "__main__":
    sys.exit(main())
