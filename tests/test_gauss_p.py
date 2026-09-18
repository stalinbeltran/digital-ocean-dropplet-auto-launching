#!/usr/bin/env python3
"""Que el descriptor de gauss-p sea coherente consigo mismo, con el mini y con la app.

    python3 tests/test_gauss_p.py

Sin framework y sin dependencias, como el resto de este repo.

QUE SE FIJA AQUI, y por que cada cosa:

1. **El PUERTO no aparece en el descriptor.** Vive en `PUERTO = 8030` de
   `nn/app.py` y en ningun sitio mas (R7, igual que sispla-demo). Escribirlo aqui
   crea un segundo sitio que puede DERIVAR del primero, y la deriva se nota
   tarde: la unidad arranca en un puerto y `url` anuncia otro -- el mismo par
   «se vigilaba una mitad y fallo la otra» que costo la web movil cuatro veces.

2. **`install` tiene que crear el venv.** El mini NO trae `python3-venv`:
   `cloud-init.mini.yaml` lo deja fuera a proposito («aqui no se desarrolla
   nunca», docs/reparto-mini-dev.md). Si alguien simplifica `install` a un `pip
   install` a secas, un mini nuevo nace con el servicio ROTO y el `AVISO` de
   provision se pierde con su log -- el fallo del 2026-09-15, que costo un dev.

3. **Que el mini lo lleve.** Un servicio que no esta en el tipo no nace con la
   maquina, y entonces «esta instalado» dura hasta el siguiente `launch`.

4. **Que la ruta del experimento EXISTA.** Es una cadena dentro de un JSON: si la
   carpeta se renombra, nada la sigue, y el sintoma es una unidad que no arranca
   en una maquina recien parida. Solo se puede comprobar con el repo al lado; si
   no esta, SE DICE en vez de callarse (el `NO SE` de siempre).

⚠ Lo que este test NO cubre: que la app haga lo que dice. Eso es suyo
(`nn/probar.py` de experimentos-cnn). Y la instalacion de verdad se midio a mano
el 2026-09-18 contra el mini vivo: unidad `active`, `Result=success`,
`NRestarts=0`, y un `curl` DESDE OTRA MAQUINA dio 200 con token y 403 sin el
-- que es lo unico que prueba «la red de en medio», la que `estado` no puede ver.
"""

import importlib.util
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APP = "2026-09-17-gauss-parametros/nn/app.py"


def cargar():
    spec = importlib.util.spec_from_file_location(
        "do_droplet", ROOT / "scripts" / "do_droplet.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def main() -> int:
    mod = cargar()
    svc = mod.load_service("gauss-p")
    fallos = 0
    total = 0

    def caso(nombre: str, ok: bool, detalle: str = "") -> None:
        nonlocal fallos, total
        total += 1
        fallos += not ok
        print(f"  {'ok   ' if ok else 'FALLO'} {nombre}{'  -> ' + detalle if detalle else ''}")

    caso("el repo es experimentos-cnn y se clona en ~/src/experimentos-cnn",
         svc["repo"] == "stalinbeltran/experimentos-cnn" and svc["dir"] == "experimentos-cnn",
         f'{svc["repo"]} -> {svc["dir"]}')

    for campo in ("install", "start", "url"):
        caso(f"{campo} delega en la app (R7): .venv/bin/python + {APP}",
             ".venv/bin/python" in svc[campo] and APP in svc[campo], svc[campo][-60:])

    # (1) El puerto vive en UN sitio. Cualquier numero de 4 digitos aqui es deriva.
    for campo in ("install", "start", "url"):
        puertos = re.findall(r"\b\d{4}\b", svc[campo].replace("2026-09-17", ""))
        caso(f"{campo} NO declara puerto (vive en PUERTO de app.py)",
             not puertos, ", ".join(puertos))

    # (2) El mini no trae python3-venv: install tiene que ponerlo, y abrir ufw.
    caso("install instala python3-venv (el mini no lo trae)",
         "python3-venv" in svc["install"])
    caso("install crea el venv", "python3 -m venv .venv" in svc["install"])
    caso("install pone numpy y pillow", "numpy" in svc["install"] and "pillow" in svc["install"])
    caso("install abre el cortafuegos en el MISMO paso",
         svc["install"].rstrip().endswith("app.py abrir"))

    # El token lo crea el servidor al arrancar: no hay nada que configurar.
    caso("sin env_prefix", not svc["env_prefix"], svc["env_prefix"])

    # (3) Un servicio que no esta en el tipo no nace con la maquina.
    mini = json.loads((ROOT / "types" / "mini.json").read_text(encoding="utf-8"))
    caso("types/mini.json lo lleva", "gauss-p" in mini.get("services", []))

    # (4) La ruta del experimento existe -- solo comprobable con el repo al lado.
    hermano = ROOT.parent / "experimentos-cnn"
    if (hermano / APP).is_file():
        caso(f"la ruta {APP} existe en el repo hermano", True)
        texto = (hermano / APP).read_text(encoding="utf-8")
        caso("y el puerto de la app sigue siendo 8030",
             "PUERTO = 8030" in texto,
             "si cambia, cambia la URL y el `ufw allow` de otros documentos")
    else:
        print(f"  NO SE  {hermano} no esta clonado: no puedo comprobar la ruta "
              f"ni el puerto. No cuenta como fallo, pero tampoco como comprobado.")

    print(f"\n{total - fallos}/{total} pasan")
    return 1 if fallos else 0


if __name__ == "__main__":
    sys.exit(main())
