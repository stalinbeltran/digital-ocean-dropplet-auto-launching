#!/usr/bin/env python3
"""Que el reinicio final de los servicios NO aborte el aprovisionamiento.

    python3 tests/test_reiniciar_servicios.py

Sin framework y sin dependencias, como el resto de este repo.

QUE SE FIJA AQUI, y por que hacia falta
---------------------------------------
`reiniciar_servicios()` se anadio el 2026-09-11 por la manana para arreglar un
fallo real -el entorno de un servicio es una foto de cuando arranco, y aqui los
servicios arrancan A MITAD del aprovisionamiento- y **venia rota**: declaraba
`services: list[str]`, pero `cmd_provision` le pasa lo que devuelve
`selected_services()`, que son **dicts**. `shq(dict)` levanta `AttributeError`,
y la lista por comprension esta FUERA del `try`, asi que no avisaba: abortaba
el `launch` con traceback.

Medido esa tarde en el dev nacido a las 15:20 UTC: la clave de flota se
escribio, ningun servicio se reinicio, y **los dos `post` del tipo no
corrieron** -ni `entornos aplicar` ni el `register-key` de Vast-, porque
`ejecutar_post()` va DESPUES. La maquina quedo pudiendo alquilar instancias de
Vast en las que no podia entrar.

Nada lo cazo porque nada lo ejecutaba: `grep -rn reiniciar_servicios tests/`
daba cero. La anotacion `list[str]` estaba escrita desde el primer commit.

Por eso el caso 1 llama a la funcion con **lo que devuelve `selected_services`**
y no con una lista de nombres a mano: una prueba que construye su propia
entrada no habria visto nada. El desajuste era entre QUIEN LLAMA y QUIEN RECIBE.
"""

import importlib.util
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
    fallos = 0

    def caso(nombre: str, ok: bool, detalle: str = "") -> None:
        nonlocal fallos
        fallos += not ok
        print(f"  {'ok   ' if ok else 'FALLO'} {nombre}{'  -> ' + detalle if detalle else ''}")

    dicho: list[str] = []
    mod.log = lambda m: dicho.append(str(m))

    visto: dict = {}

    def falso_remoto(ip, port, guion, timeout=None):
        visto["guion"] = guion
        return 0

    mod.run_remote_script = falso_remoto

    # --- 1. EL CASO QUE FALLA CON EL CODIGO ANTERIOR ----------------------
    # Se le pasa exactamente lo que le pasa cmd_provision.
    servicios = mod.selected_services(["telegram-launcher"])
    try:
        mod.reiniciar_servicios("1.2.3.4", 22, servicios)
        caso("no revienta con lo que devuelve selected_services()", True)
    except Exception as exc:  # noqa: BLE001 - es justo lo que se mide
        caso("no revienta con lo que devuelve selected_services()", False,
             f"{type(exc).__name__}: {exc}")
        visto["guion"] = ""

    # --- 2. reinicia el UNIT, no el repr de un dict -----------------------
    guion = visto.get("guion", "")
    # shq() entrecomilla, que es lo correcto: se compara contra lo que produce.
    caso("el guion reinicia el unit por su nombre",
         f"systemctl restart {mod.shq('telegram-launcher')}" in guion,
         f"guion={guion[:70]!r}")
    caso("no se cuela la representacion de un dict",
         "descripcion" not in guion and "{'" not in guion,
         "un dict impreso dejaria el systemctl sin sentido")

    # --- 3. estan TODOS los servicios declarados --------------------------
    visto.clear()
    varios = mod.selected_services(["telegram-coordinator", "foveal-vision-web"])
    try:
        mod.reiniciar_servicios("1.2.3.4", 22, varios)
    except Exception:  # noqa: BLE001 - con el codigo roto revienta; lo reporta el caso
        pass
    g = visto.get("guion", "")
    caso("reinicia todos los servicios, no solo el primero",
         all(f"systemctl restart {mod.shq(s['name'])}" in g for s in varios),
         f"{len(varios)} declarados")

    # --- 4. un fallo del reinicio AVISA, nunca tumba el launch ------------
    # Esto ya funcionaba y se fija para no romperlo al arreglar lo de arriba:
    # cuando esto corre la maquina ya existe, y morir aqui la dejaria hecha
    # pero con el launch en traceback.
    def remoto_que_falla(ip, port, guion, timeout=None):
        raise RuntimeError("sshd se cayo")

    mod.run_remote_script = remoto_que_falla
    dicho.clear()
    try:
        mod.reiniciar_servicios("1.2.3.4", 22, varios)
        caso("un fallo del reinicio no aborta", True)
    except Exception as exc:  # noqa: BLE001
        caso("un fallo del reinicio no aborta", False, f"{type(exc).__name__}: {exc}")
        dicho.append("AVISO")  # para que el caso siguiente mida lo suyo, no esto
    caso("y ademas lo dice",
         any("AVISO" in m for m in dicho),
         "un reinicio perdido en silencio es el fallo que esto arregla")

    # --- 5. sin servicios no se toca la maquina ---------------------------
    visto.clear()
    mod.run_remote_script = falso_remoto
    mod.reiniciar_servicios("1.2.3.4", 22, [])
    caso("sin servicios no hay llamada remota", "guion" not in visto)

    total = 7
    print(f"\n{total - fallos}/{total} pasan")
    return 1 if fallos else 0


if __name__ == "__main__":
    sys.exit(main())
