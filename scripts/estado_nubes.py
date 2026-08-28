#!/usr/bin/env python3
"""Qué hay vivo AHORA en las dos nubes, con su gasto por hora.

Es la unión de `do_droplet.py list` y `vast_instance.py list`, y existe para que
esa unión esté escrita en **un** sitio. La piden dos ejecutores del bot —`estado`
y el `list` de `lanzar`—, y con la orden copiada en los dos JSON, añadir un
proveedor arreglaría uno y dejaría el otro contando de menos: una nube olvidada
no da error, sigue facturando en silencio.

    python scripts/estado_nubes.py
    python scripts/estado_nubes.py --exit0     # para el bot; ver abajo

**No comparte código con ninguno de los dos: los invoca como procesos.** Es
deliberado (objetivo 12 del CLAUDE.md: «los dos scripts no comparten código a
propósito»), y además es lo que permite que una nube ilegible no se lleve por
delante la salida de la otra.

Sale con 0 si pudo consultar las dos, y 1 si alguna no contestó. Ojo con eso
desde Telegram: el coordinador lee cualquier código != 0 como «el ejecutor
falló», y entonces no corre los encargados y **no llega nada al chat** — se
perdería justo la mitad que sí se pudo leer. Por eso los ejecutores lo llaman
con `--exit0` y el aviso va en el **texto**, que es donde se lee. Es la misma
decisión que `telegram-coordinator/scripts/cerrable.mjs`.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent

# Un proveedor nuevo se añade aquí y lo heredan los dos ejecutores.
NUBES = (
    ("DigitalOcean", SCRIPTS / "do_droplet.py", ["list"]),
    ("Vast.ai", SCRIPTS / "vast_instance.py", ["list"]),
)


def listar(nombre: str, script: Path, args: list[str]) -> tuple[str, bool]:
    """Devuelve (texto, ok). Nunca lanza: una nube ilegible no puede tapar la otra."""
    try:
        r = subprocess.run(
            [sys.executable, str(script), *args],
            capture_output=True,
            text=True,
        )
    except OSError as e:  # el script no está, o no se puede ejecutar
        return f"⚠ {nombre}: no se pudo ejecutar {script.name} ({e})", False

    salida = (r.stdout + r.stderr).strip()
    if r.returncode == 0:
        return salida, True

    # Un fallo se ANUNCIA como fallo. Sin esta línea, «no hay nada alquilado» y
    # «no pude mirar» se leen igual, y el segundo es el que cuesta dinero.
    aviso = f"⚠ {nombre}: no se pudo consultar (código {r.returncode})"
    return f"{aviso}\n{salida}".strip(), False


def main() -> int:
    p = argparse.ArgumentParser(description="Qué hay vivo en las dos nubes.")
    p.add_argument(
        "--exit0",
        action="store_true",
        help="salir siempre con 0 (para el bot: un código != 0 lo lee como fallo "
        "del ejecutor y entonces no llega nada a Telegram)",
    )
    args = p.parse_args()

    bloques: list[str] = []
    todo_ok = True
    for nombre, script, sub in NUBES:
        texto, ok = listar(nombre, script, sub)
        todo_ok = todo_ok and ok
        if texto:
            bloques.append(texto)

    print("\n\n".join(bloques))
    return 0 if (todo_ok or args.exit0) else 1


if __name__ == "__main__":
    sys.exit(main())
