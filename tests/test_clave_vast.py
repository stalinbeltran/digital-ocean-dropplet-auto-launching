#!/usr/bin/env python3
"""Un proveedor, una clave: las rutas por defecto de los dos no pueden coincidir.

    python3 tests/test_clave_vast.py

Sin framework ni dependencias, como el resto.

Por que esto merece un test propio, siendo una linea. Hasta el 2026-09-11
`VAST_SSH_KEY_FILE` y `DO_SSH_KEY_FILE` apuntaban los dos a `~/.ssh/do_droplet`,
y estaba puesto **a proposito**: el `.env.example` lo explicaba como "por defecto
la misma que la de DigitalOcean", y cuando se escribio era razonable, porque los
droplets se entraban con esa clave.

Dejo de ser razonable al llegar la CLAVE DE FLOTA. DigitalOcean se movio a
`~/.ssh/do_flota` y `do_droplet` quedo como un fichero que EXISTE y no autentica
contra DO -- y lo peor: lo CREA el `post` de los tipos, que corre
`vast_instance.py register-key`, asi que toda maquina de la flota nacia con esa
trampa puesta en la ruta por defecto de DigitalOcean. El 2026-09-11 un `launch
mini` desde el bot de un dev murio ahi, y "si la clave no existe, usa la de la
flota" no lo arreglaba, porque el fichero SI existia.

O sea que la coincidencia no se nota cuando se introduce, se nota meses despues
y desde un chat de Telegram. De ahi el test: es la clase de linea que alguien
vuelve a igualar por comodidad.
"""

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def cargar(nombre: str):
    spec = importlib.util.spec_from_file_location(
        nombre, ROOT / "scripts" / f"{nombre}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[nombre] = mod  # vast_instance importa dataset por su nombre
    spec.loader.exec_module(mod)
    mod.log = lambda *_a, **_k: None
    return mod


def test_las_dos_rutas_son_distintas():
    do = cargar("do_droplet")
    vast = cargar("vast_instance")
    a = Path(do.DEFAULTS["DO_SSH_KEY_FILE"]).expanduser()
    b = Path(vast.DEFAULTS["VAST_SSH_KEY_FILE"]).expanduser()
    if a == b:
        return [f"los dos proveedores comparten la ruta por defecto: {a}"]
    return []


def test_vast_no_apunta_a_la_ruta_de_do():
    """Ni por la otra punta: el defecto de Vast no puede ser el de DO.

    Se compara contra el LITERAL y no solo contra el otro defecto, porque si
    alguien cambiara los dos a la vez el test de arriba seguiria pasando.
    """
    vast = cargar("vast_instance")
    b = Path(vast.DEFAULTS["VAST_SSH_KEY_FILE"]).expanduser()
    if b.name == "do_droplet" or b.name == "do_flota":
        return [f"la clave de Vast se llama como una de DigitalOcean: {b}"]
    return []


def test_el_env_example_no_las_vuelve_a_igualar():
    """La plantilla es lo que la gente copia, asi que ahi tambien.

    Un `.env` con la vieja ruta pisa el defecto, y entonces el arreglo no
    existe en esa maquina.
    """
    texto = (ROOT / ".env.example").read_text(encoding="utf-8")
    fallos = []
    for linea in texto.splitlines():
        limpia = linea.strip()
        if limpia.startswith("VAST_SSH_KEY_FILE=") and "do_droplet" in limpia:
            fallos.append(f".env.example vuelve a igualarlas: {limpia}")
    return fallos


def main():
    pruebas = [
        ("las dos rutas por defecto son distintas", test_las_dos_rutas_son_distintas),
        ("la de Vast no se llama como una de DO", test_vast_no_apunta_a_la_ruta_de_do),
        ("el .env.example no las vuelve a igualar",
         test_el_env_example_no_las_vuelve_a_igualar),
    ]
    total = 0
    for nombre, prueba in pruebas:
        fallos = prueba()
        total += len(fallos)
        print(f"  {'ok   ' if not fallos else 'FALLO'} {nombre}")
        for fallo in fallos:
            print(f"          {fallo}")
    print(f"\n{len(pruebas)} pruebas, {total} fallo(s)")
    return 1 if total else 0


if __name__ == "__main__":
    sys.exit(main())
