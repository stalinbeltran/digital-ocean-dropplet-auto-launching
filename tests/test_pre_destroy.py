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
import json
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
    # ⚠ Hasta el 2026-09-12 este caso miraba `claude-web`, que era el unico que
    # declaraba el gancho (lo usaba para dar de baja el nodo de Tailscale). Con
    # «cero tailscale» esa app ya no tiene nada que recoger al destruir, y un test
    # que exige un gancho a un servicio que no lo necesita obliga a inventarse uno.
    # Se prueba el MECANISMO con un descriptor propio, que es lo que de verdad no
    # puede romperse: el gancho es DATO, y se lee de donde se declara.
    with tempfile.TemporaryDirectory() as tmp:
        carpeta = Path(tmp)
        (carpeta / "conGancho.json").write_text(json.dumps(
            {"repo": "x/y", "install": "true", "start": "true",
             "pre_destroy": "node scripts/recoger.mjs --si"}), encoding="utf-8")
        antes = mod.SERVICES_DIR
        mod.SERVICES_DIR = carpeta
        try:
            svc = mod.load_service("conGancho")
            caso("un descriptor que declara pre_destroy se lee tal cual",
                 svc.get("pre_destroy") == "node scripts/recoger.mjs --si",
                 svc.get("pre_destroy", ""))
        finally:
            mod.SERVICES_DIR = antes

    svc = mod.load_service("claude-web")
    caso("claude-web YA NO declara pre_destroy (cero tailscale, 2026-09-12)",
         svc.get("pre_destroy", "") == "",
         "no tiene nada que recoger: no se une a ninguna tailnet")

    otro = mod.load_service("telegram-coordinator")
    caso("un servicio que no lo declara se queda a cero",
         otro.get("pre_destroy") == "",
         "el campo existe con defecto vacio, asi que nadie tiene que declararlo")

    # ⚠ El guion se comprueba contra un descriptor PROPIO desde el 2026-09-12.
    # Antes se apoyaba en que `claude-web` declarara un gancho, y al quitarselo
    # (cero tailscale) este mecanismo se quedo SIN NINGUN USUARIO en el repo: el
    # test pasaba a medir un guion vacio sin decirlo. Un mecanismo generico se
    # prueba con su propio caso, no con quien casualmente lo use hoy.
    with tempfile.TemporaryDirectory() as tmp:
        carpeta = Path(tmp)
        (carpeta / "conGancho.json").write_text(json.dumps(
            {"repo": "x/y", "dir": "y", "install": "true", "start": "true",
             "pre_destroy": "node scripts/recoger.mjs --si"}), encoding="utf-8")
        (carpeta / "sinGancho.json").write_text(json.dumps(
            {"repo": "a/b", "dir": "b", "install": "true", "start": "true"}), encoding="utf-8")
        antes = mod.SERVICES_DIR
        mod.SERVICES_DIR = carpeta
        try:
            guion = mod.pre_destroy_script()
        finally:
            mod.SERVICES_DIR = antes

    caso("el guion no cablea ningun proyecto en el lanzador",
         "recoger.mjs" in guion and "tailscale" not in guion.lower(),
         "solo aparece lo que declara el SERVICIO, nunca un nombre propio aqui")
    caso("el guion se salta el servicio que no esta instalado",
         "nada que recoger" in guion)

    # Y con CERO ganchos declarados no se inventa trabajo: es el estado real del
    # repo desde hoy, y tiene que ser un guion que no haga nada, no uno roto.
    with tempfile.TemporaryDirectory() as tmp:
        carpeta = Path(tmp)
        (carpeta / "sinGancho.json").write_text(json.dumps(
            {"repo": "a/b", "dir": "b", "install": "true", "start": "true"}), encoding="utf-8")
        antes = mod.SERVICES_DIR
        mod.SERVICES_DIR = carpeta
        try:
            vacio = mod.pre_destroy_script()
        finally:
            mod.SERVICES_DIR = antes
    caso("sin ningun gancho declarado el guion no rompe",
         isinstance(vacio, str),
         f"{len(vacio)} caracteres")

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
