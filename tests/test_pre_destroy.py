#!/usr/bin/env python3
"""Que la recogida previa al destroy NUNCA impida destruir el droplet.

    python3 tests/test_pre_destroy.py

Sin framework y sin dependencias, como el resto de este repo.

QUE SE FIJA AQUI, y por que estas cosas y no que "funcione": el gancho
`pre_destroy` corre por SSH contra una maquina que esta a punto de morir, o
sea el sitio con mas formas de salir mal de todo el repo -sin IP, SSH caido,
el comando colgado, el servicio ni instalado-. En TODAS ellas el droplet
tiene que destruirse igual: si la recogida pudiera tumbar el apagado, una
molestia (que el nombre del nodo siga ocupado ~75 min) se convertiria en una
factura (un droplet vivo que nadie apaga).

Es la leccion del 2026-09-04 -si el aviso puede matar el trabajo, ya no es una
comodidad- llevada a la estructura.

⚠ Lo que este test NO cubre: que `tailscale logout` borre el nodo de verdad.
Eso se midio a mano el 2026-09-10 en este dev (Self ID nznvvWsNKE11CNTRL ->
nbjCKGjnuo11CNTRL, 3 nodos antes y 3 despues, nombre `dev-1` reutilizado en
2 s) y esta escrito en claude-code-webapp-mobile/scripts/nodo.mjs.
"""

import importlib.util
import sys
import tempfile
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
    all_services_real = mod.all_services
    fallos = 0

    def caso(nombre: str, ok: bool, detalle: str = "") -> None:
        nonlocal fallos
        fallos += not ok
        print(f"  {'ok   ' if ok else 'FALLO'} {nombre}{'  -> ' + detalle if detalle else ''}")

    # --- el gancho es DATO, no codigo -------------------------------------
    svc = mod.load_service("claude-web")
    caso("claude-web declara pre_destroy", bool(svc.get("pre_destroy")), svc.get("pre_destroy", ""))

    otro = mod.load_service("telegram-coordinator")
    caso("un servicio que no lo declara se queda a cero",
         otro.get("pre_destroy") == "",
         "el campo existe con defecto vacio, asi que nadie tiene que declararlo")

    guion = mod.pre_destroy_script()
    caso("el guion no cablea Tailscale en el lanzador",
         "tailscale" not in guion.lower().replace("tailscale-desunir", ""),
         "solo aparece dentro del comando que declara el SERVICIO")
    caso("el guion se salta el servicio que no esta instalado",
         "nada que recoger" in guion)

    # --- R2: pase lo que pase, el droplet se destruye ----------------------
    # Cada rama de fallo se fuerza y se comprueba que la funcion VUELVE (no
    # levanta), porque quien la llama destruye justo despues.
    ramas = []

    ramas.append(("sin IP", {"name": "x", "networks": {"v4": []}}, None, None))

    def sin_ssh(ip, timeout=300):
        return None

    def ssh_que_revienta(ip, timeout=300):
        raise SystemExit("no hay clave")

    def ssh_ok(ip, timeout=300):
        return 22

    def remoto_colgado(ip, port, script, timeout=None):
        raise TimeoutError("se colgo")

    def remoto_revienta(ip, port, script, timeout=None):
        raise OSError("la conexion se cayo a mitad")

    con_ip = {"name": "x", "networks": {"v4": [{"type": "public", "ip_address": "1.2.3.4"}]}}
    ramas += [
        ("SSH no contesta", con_ip, sin_ssh, None),
        ("wait_for_ssh levanta SystemExit", con_ip, ssh_que_revienta, None),
        ("el comando remoto se cuelga", con_ip, ssh_ok, remoto_colgado),
        ("la conexion se cae a mitad", con_ip, ssh_ok, remoto_revienta),
    ]

    for nombre, droplet, fake_ssh, fake_remoto in ramas:
        if fake_ssh:
            mod.wait_for_ssh = fake_ssh
        if fake_remoto:
            mod.run_remote_script = fake_remoto
        try:
            mod.limpiar_antes_de_destruir(droplet, timeout=1)
            caso(f"vuelve con {nombre}", True)
        except BaseException as exc:  # noqa: BLE001 - eso es justo lo que no puede pasar
            caso(f"vuelve con {nombre}", False, f"levanto {type(exc).__name__}: {exc}")

    # --- ⚠⚠ un descriptor ROTO no puede abortar el destroy -----------------
    # `load_service()` hace `die()` -> SystemExit si un services/*.json esta
    # roto, y `pre_destroy_script()` los recorre TODOS. Con la construccion del
    # guion fuera del try, un fichero mal editado -de un servicio que ni siquiera
    # esta en este droplet- abortaba cmd_destroy ANTES de borrar nada, y dejaba
    # droplets vivos que nadie apaga. Este caso FALLA con esa version.
    def descriptor_roto():
        raise SystemExit("services/loquesea.json no es JSON válido")

    mod.all_services = descriptor_roto
    try:
        mod.limpiar_antes_de_destruir(con_ip, timeout=1)
        caso("un descriptor roto NO aborta el destroy", True)
    except BaseException as exc:  # noqa: BLE001
        caso("un descriptor roto NO aborta el destroy", False,
             f"levanto {type(exc).__name__}: un JSON mal editado dejaria droplets vivos")

    # Y lo simetrico: si TU cortas, se corta. KeyboardInterrupt no se traga.
    def cortado():
        raise KeyboardInterrupt()

    mod.all_services = cortado
    try:
        mod.limpiar_antes_de_destruir(con_ip, timeout=1)
        caso("Ctrl-C SI se propaga", False, "tragarselo ignoraria que quieres parar")
    except KeyboardInterrupt:
        caso("Ctrl-C SI se propaga", True)
    except BaseException as exc:  # noqa: BLE001
        caso("Ctrl-C SI se propaga", False, f"levanto {type(exc).__name__}")

    # --- ⚠ y con un descriptor roto DE VERDAD en disco ---------------------
    # El caso de arriba sustituye `all_services` por una funcion que levanta, o
    # sea que fija el CONTRATO pero no recorre el `load_service` real. Aqui se
    # escribe un services/*.json genuinamente malformado y se apunta
    # SERVICES_DIR a el, para ejercitar el camino entero: glob -> load_service
    # -> json.JSONDecodeError -> die() -> SystemExit. Es la diferencia entre
    # «el mock dice que levanta» y «un fichero mal editado hace esto».
    mod.all_services = all_services_real
    with tempfile.TemporaryDirectory() as tmp:
        carpeta = Path(tmp)
        (carpeta / "roto.json").write_text('{"repo": "x/y", "start": ', encoding="utf-8")
        mod.SERVICES_DIR = carpeta
        try:
            mod.limpiar_antes_de_destruir(con_ip, timeout=1)
            caso("un services/*.json roto DE VERDAD no aborta el destroy", True)
        except BaseException as exc:  # noqa: BLE001
            caso("un services/*.json roto DE VERDAD no aborta el destroy", False,
                 f"levanto {type(exc).__name__}")

        # Y el simetrico: un descriptor VALIDO sin `pre_destroy` no hace nada.
        (carpeta / "roto.json").unlink()
        (carpeta / "sano.json").write_text(
            '{"repo": "x/y", "start": "node s.js"}', encoding="utf-8")
        tocado_sano = []
        mod.wait_for_ssh = lambda ip, timeout=300: tocado_sano.append("ssh") or 22
        mod.limpiar_antes_de_destruir(con_ip, timeout=1)
        caso("un descriptor sano sin pre_destroy no abre conexion", not tocado_sano)

    # --- y si NADIE declara pre_destroy, ni se conecta ---------------------
    mod.all_services = lambda: []
    tocado = []
    mod.wait_for_ssh = lambda ip, timeout=300: tocado.append("ssh") or 22
    mod.limpiar_antes_de_destruir(con_ip, timeout=1)
    caso("sin ningun pre_destroy no se abre ni la conexion", not tocado,
         "abrir SSH para no hacer nada retrasa cada destroy")

    total = 4 + len(ramas) + 5
    print(f"\n{total - fallos}/{total} pasan")
    return 1 if fallos else 0


if __name__ == "__main__":
    sys.exit(main())
