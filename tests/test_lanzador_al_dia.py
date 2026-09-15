#!/usr/bin/env python3
"""Que no se lance una maquina con la receta VIEJA.

    python3 tests/test_lanzador_al_dia.py

Sin framework y sin dependencias, igual que test_provision_incompleto.py: este
repo corre con el python3 pelado del sistema.

EL FALLO QUE SE FIJA AQUI, medido el 2026-09-15. El `mini` pario un `dev` con
su copia de este repo SEIS commits atras -HEAD del 11-sep, o sea la era de
Tailscale-. `services/claude-web.json` decia todavia
`install: node scripts/acceso.mjs unir`, y ese script se habia borrado el 12-sep
al quitar Tailscale entero. Cadena completa:

    install -> MODULE_NOT_FOUND -> `|| echo AVISO` -> log del provision (que no
    sobrevive) -> provision sale con 0 -> nadie se entera

La maquina nacio con la web movil atada a 0.0.0.0:8020 y ese puerto CERRADO en
ufw, porque quien lo abre es el `cweb instalar` que nunca corrio. Desde el movil
se vio como «la URL no carga» -indistinguible de un token malo-, asi que se
depuro la app, que estaba bien, y el arreglo de verdad llevaba TRES DIAS en
`main`.

Lo que hace especial a este fallo, y por lo que el freno va aqui y no en la
maquina nueva: **el dato que falta no esta en la maquina que nace, esta en quien
la pare.** Ninguna comprobacion hecha dentro del dev podia verlo.

Cuatro contratos:

 1. `estado_lanzador` distingue los tres estados contra repos git DE VERDAD;
 2. "viejo" MATA -antes de crear nada- y el mensaje trae el comando que arregla;
 3. la DUDA no mata, que es la diferencia entre proteger y estorbar;
 4. `cmd_launch` lo llama ANTES del POST que crea el droplet.
"""

import contextlib
import importlib.util
import inspect
import io
import shutil
import subprocess
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


def comprueba(nombre, condicion, fallos):
    print(f"  {'ok   ' if condicion else 'FALLO'} {nombre}")
    return fallos + (not condicion)


def git(cwd, *a):
    return subprocess.run(["git", "-C", str(cwd), *a],
                          capture_output=True, text=True, timeout=60)


def repo_con_clon(tmp):
    """Un 'remoto' y un clon suyo. Devuelve (remoto, clon) o None si no hay git."""
    remoto, clon = Path(tmp) / "remoto", Path(tmp) / "clon"
    remoto.mkdir()
    if git(remoto, "init", "-q", "-b", "main").returncode != 0:
        return None
    git(remoto, "config", "user.email", "t@t")
    git(remoto, "config", "user.name", "t")
    (remoto / "a.txt").write_text("1")
    git(remoto, "add", "-A")
    git(remoto, "commit", "-qm", "uno")
    if subprocess.run(["git", "clone", "-q", str(remoto), str(clon)],
                      capture_output=True, timeout=60).returncode != 0:
        return None
    git(clon, "config", "user.email", "t@t")
    git(clon, "config", "user.name", "t")
    return remoto, clon


def bloque_1_estados(mod, fallos):
    print("\n1. los tres estados, contra repos git de verdad")
    if not shutil.which("git"):
        print("  (saltado: no hay git)")
        return fallos
    with tempfile.TemporaryDirectory() as tmp:
        par = repo_con_clon(tmp)
        if par is None:
            print("  (saltado: git no pudo crear el repo de prueba)")
            return fallos
        remoto, clon = par

        mod.ROOT = clon
        fallos = comprueba("un clon recien hecho esta al dia",
                           mod.estado_lanzador()[0] == "ok", fallos)

        # El remoto avanza DOS commits y el clon no se entera: es exactamente
        # lo que le pasaba al mini.
        for i in (2, 3):
            (remoto / "a.txt").write_text(str(i))
            git(remoto, "add", "-A")
            git(remoto, "commit", "-qm", f"commit {i}")
        estado, detalle = mod.estado_lanzador()
        fallos = comprueba("con el remoto por delante dice 'viejo'",
                           estado == "viejo", fallos)
        fallos = comprueba("y dice CUANTOS commits (2) y de que rama",
                           detalle == "2|main", fallos)

        # Tras actualizar vuelve a estar al dia: el freno se puede QUITAR de en
        # medio haciendo lo que pide, no solo con la opcion de emergencia.
        git(clon, "pull", "-q", "--ff-only")
        fallos = comprueba("tras el pull vuelve a 'ok'",
                           mod.estado_lanzador()[0] == "ok", fallos)

        # Un directorio que no es un repo: duda, nunca "ok". Decir "al dia" de
        # algo que no se ha podido mirar es el falso verde de siempre.
        mod.ROOT = Path(tmp)
        fallos = comprueba("lo que no es un repo git es DUDA, no 'ok'",
                           mod.estado_lanzador()[0] == "duda", fallos)
    mod.ROOT = ROOT
    return fallos


def bloque_2_bloquea(mod, fallos):
    print("\n2. 'viejo' mata, y el mensaje trae el arreglo")
    muertes = []
    mod.die = lambda m: (_ for _ in ()).throw(SystemExit(m))

    mod.estado_lanzador = lambda *a, **k: ("viejo", "6|main")
    try:
        mod.comprobar_lanzador_al_dia()
        muertes.append(None)
    except SystemExit as e:
        muertes.append(str(e))
    msg = muertes[0]
    fallos = comprueba("un lanzador viejo NO deja lanzar", msg is not None, fallos)
    fallos = comprueba("dice cuantos commits le faltan",
                       bool(msg) and "6 commit" in msg, fallos)
    fallos = comprueba("trae el comando que lo arregla",
                       bool(msg) and "git pull --ff-only" in msg, fallos)
    fallos = comprueba("y la salida de emergencia",
                       bool(msg) and "--sin-version" in msg, fallos)

    # Con la opcion de emergencia no mira nada y deja pasar.
    mod.estado_lanzador = lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("con --sin-version no debe ni preguntar"))
    try:
        mod.comprobar_lanzador_al_dia(True)
        paso = True
    except (SystemExit, AssertionError):
        paso = False
    fallos = comprueba("--sin-version deja pasar sin preguntar", paso, fallos)
    return fallos


def bloque_3_duda(mod, fallos):
    print("\n3. la duda NO mata (proteger no es estorbar)")
    mod.die = lambda m: (_ for _ in ()).throw(SystemExit(m))
    for detalle in ("no parece un repo git", "el fetch falló", "Temporary failure"):
        mod.estado_lanzador = lambda *a, **k: ("duda", detalle)
        try:
            mod.comprobar_lanzador_al_dia()
            paso = True
        except SystemExit:
            paso = False
        fallos = comprueba(f"duda ({detalle[:24]}…) avisa y sigue", paso, fallos)

    mod.estado_lanzador = lambda *a, **k: ("ok", "main")
    try:
        mod.comprobar_lanzador_al_dia()
        paso = True
    except SystemExit:
        paso = False
    fallos = comprueba("al dia deja pasar", paso, fallos)
    return fallos


def bloque_4_orden(mod, fallos):
    print("\n4. se comprueba ANTES de crear el droplet")
    cuerpo = inspect.getsource(mod.cmd_launch)
    hay = "comprobar_lanzador_al_dia" in cuerpo
    fallos = comprueba("cmd_launch lo llama", hay, fallos)
    if hay:
        crea = cuerpo.find('api("POST", "/v2/droplets"')
        fallos = comprueba(
            "y lo llama antes del POST que crea la maquina",
            crea > 0 and cuerpo.find("comprobar_lanzador_al_dia") < crea, fallos)
        # Va el PRIMERO del bloque: es local y no depende de una API ajena.
        fallos = comprueba(
            "antes incluso que la clave de entrada (es lo mas barato)",
            cuerpo.find("comprobar_lanzador_al_dia")
            < cuerpo.find("comprobar_clave_de_entrada"), fallos)
    return fallos


def main():
    mod = cargar()
    mod.log = lambda msg: None
    fallos = 0
    with contextlib.redirect_stderr(io.StringIO()):
        fallos = bloque_1_estados(mod, fallos)
        fallos = bloque_2_bloquea(mod, fallos)
        fallos = bloque_3_duda(mod, fallos)
        fallos = bloque_4_orden(mod, fallos)
    print("\nTODO OK" if not fallos else f"\n{fallos} FALLOS")
    return 1 if fallos else 0


if __name__ == "__main__":
    sys.exit(main())
