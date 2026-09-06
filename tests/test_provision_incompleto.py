#!/usr/bin/env python3
"""Que una maquina que nace a medias lo DIGA, y que un token muerto pare antes.

    python3 tests/test_provision_incompleto.py

Sin framework y sin dependencias, igual que test_url_servicio.py: este repo
corre con el python3 pelado del sistema.

Lo que se fija aqui es la cadena entera del fallo del 2026-09-06, que costo un
dia entero de diagnostico y una maquina rota: el GITHUB_TOKEN del `mini` estaba
revocado, `provision` no pudo clonar un repo privado, lo dijo con un AVISO en
mitad de cien lineas de salida y **salio con codigo 0**, asi que el lanzamiento
dio el trabajo por bueno. La maquina nacio sin el sitio donde se guarda lo
medido y nadie se entero hasta el primer `git push`.

Tres contratos, y cada uno tapa un tramo distinto:

 1. el script generado apunta lo que no pudo clonar y sale con
    PROVISION_INCOMPLETO, no con 0;
 2. `cmd_provision` traduce ese codigo: devuelve False cuando lo llama `launch`
    (que todavia tiene que imprimir la IP y la orden de destruir) y muere
    cuando lo llama una persona;
 3. `comprobar_github_token` pregunta a GitHub si el token SIRVE y bloquea si
    lo rechaza -pero NO si la respuesta es "no lo se", que es la diferencia
    entre proteger y estorbar.
"""

import argparse
import contextlib
import importlib.util
import io
import shutil
import subprocess
import sys
import urllib.error
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


def bash_utilizable():
    """Hay `bash` Y arranca.

    En PowerShell el primer `bash` del PATH suele ser el de WSL, que sin
    distribucion instalada esta pero falla con stderr vacio: preguntar solo por
    `which` daba un FALLO que no era del codigo probado (medido el 2026-09-06,
    el mismo test pasaba en Git Bash y fallaba en PowerShell).
    """
    if not shutil.which("bash"):
        return False
    try:
        return subprocess.run(["bash", "-c", "exit 0"], capture_output=True,
                              timeout=30).returncode == 0
    except OSError:
        return False


def script_generado(mod):
    return mod.build_provision_script(
        ["stalinbeltran/foveal-vision", "stalinbeltran/foveal-vision-data"],
        [],
        False,
        [],
        "github_pat_loquesea",
    )


def bloque_1_script(mod, fallos):
    print("\n1. el script generado cobra los repos que faltan")
    s = script_generado(mod)

    # Lo que habia antes: un echo y a otra cosa. Si vuelve, este test canta.
    fallos = comprueba(
        "un clone fallido ya no se queda en un echo",
        "AVISO: no pude clonar" not in s, fallos)
    fallos = comprueba(
        "cada repo apunta su fallo en FALTAN",
        s.count('|| FALTAN="$FALTAN ') == 2, fallos)
    fallos = comprueba(
        f"sale con {mod.PROVISION_INCOMPLETO} si falta alguno",
        f"exit {mod.PROVISION_INCOMPLETO}" in s, fallos)
    # Por stderr y no por stdout: cuando el comando lo lanza el bot, el
    # coordinador tira stdout y solo publica stderr si el codigo no es 0.
    fallos = comprueba(
        "y lo cuenta por stderr, que es lo que llega al chat",
        'echo "La máquina quedó A MEDIAS: no pude clonar:$FALTAN" >&2' in s, fallos)

    # Sin repos no hay FALTAN que valga: la variable no puede quedar suelta.
    sin_repos = mod.build_provision_script([], [], False, [], "")
    fallos = comprueba(
        "sin repos no se inventa la comprobacion",
        "FALTAN" not in sin_repos, fallos)

    if bash_utilizable():
        # El script viaja por stdin a un `bash -s` del droplet, asi que aqui se
        # comprueba igual: por stdin y no por un fichero temporal, que en
        # Windows obligaria a traducir la ruta. Un parentesis mal puesto aqui es
        # una maquina a medias, no un error de Python.
        r = subprocess.run(["bash", "-n"], input=s.encode("utf-8"),
                           capture_output=True)
        fallos = comprueba(
            "y es sh valido (bash -n)",
            r.returncode == 0, fallos)
        if r.returncode != 0:
            print(f"        {r.stderr.decode('utf-8', 'replace').strip()}")
    else:
        print("  (sin bash utilizable: me salto la sintaxis del script)")
    return fallos


def bloque_2_codigo(mod, fallos):
    print("\n2. cmd_provision traduce el codigo segun quien pregunte")

    mod.resolve_target = lambda name, port: (1, "1.2.3.4", 22)
    mod.run_remote_script = lambda ip, port, s: mod.PROVISION_INCOMPLETO
    mod.selected_services = lambda nombres: []

    def args(**extra):
        base = dict(name="dev", port=22, repo=["a/b"], service=[],
                    push_do_token=False, push_env=[], make_launcher=False,
                    skip_wait=True, sin_github=True, desde_launch=False)
        base.update(extra)
        return argparse.Namespace(**base)

    # Desde launch: no puede morir aqui. El droplet ya existe y factura, y el
    # resumen con su IP y con la orden de destruirla aun no se ha impreso.
    ok = mod.cmd_provision(args(desde_launch=True)) is False
    fallos = comprueba("desde launch devuelve False, sin morir", ok, fallos)

    # Llamado a pelo si muere: es lo que espera quien lo escribe, y lo unico
    # que el bot publica en el chat.
    try:
        mod.cmd_provision(args())
        murio = False
    except SystemExit:
        murio = True
    fallos = comprueba("llamado a pelo muere", murio, fallos)

    # Y lo que ya funcionaba tiene que seguir: 0 es 0.
    mod.run_remote_script = lambda ip, port, s: 0
    fallos = comprueba(
        "un aprovisionamiento entero devuelve True",
        mod.cmd_provision(args(desde_launch=True)) is True, fallos)
    return fallos


def bloque_3_token(mod, fallos):
    print("\n3. el token de GitHub se comprueba, no se supone")
    TOK = "github_pat_loquesea"

    def responde(estado):
        """Sustituye a urlopen: 'ok', un HTTPError o una red que no va."""
        def falso(req, timeout=0):
            if isinstance(estado, int):
                raise urllib.error.HTTPError(
                    "https://api.github.com/user", estado, "no", {}, None)
            if estado == "sin_red":
                raise urllib.error.URLError("no hay ruta al host")
            class Resp:
                def read(self_): return b'{"login": "stalinbeltran"}'
                def __enter__(self_): return self_
                def __exit__(self_, *a): return False
            return Resp()
        mod._GITHUB_ESTADO = None          # la cache es por proceso
        mod.urllib.request.urlopen = falso

    import os
    os.environ["GITHUB_TOKEN"] = TOK

    responde("ok")
    fallos = comprueba(
        "un token bueno pasa y se devuelve",
        mod.comprobar_github_token() == TOK, fallos)

    # El caso del 2026-09-06. Bloquea: es lo unico que evita la maquina rota.
    responde(401)
    try:
        mod.comprobar_github_token()
        murio = False
    except SystemExit:
        murio = True
    fallos = comprueba("un 401 bloquea antes de crear nada", murio, fallos)

    # Y no saber no es saber que va mal: un rate limit o una red caida no
    # pueden dejarte sin poder lanzar. Avisan y siguen.
    for estado, etiqueta in ((403, "un 403 (rate limit)"), ("sin_red", "sin red")):
        responde(estado)
        try:
            paso = mod.comprobar_github_token() == TOK
        except SystemExit:
            paso = False
        fallos = comprueba(f"{etiqueta} avisa pero no bloquea", paso, fallos)

    # La salida de emergencia: sin token no se envia nada y no se pregunta.
    responde(401)
    fallos = comprueba(
        "--sin-github no envia token ni consulta",
        mod.comprobar_github_token(sin_github=True) == "", fallos)

    os.environ["GITHUB_TOKEN"] = ""
    responde(401)
    fallos = comprueba(
        "sin GITHUB_TOKEN avisa y sigue (repos publicos)",
        mod.comprobar_github_token() == "", fallos)
    return fallos


def main():
    mod = cargar()
    # Que los avisos no ensucien la lista de resultados.
    mod.log = lambda msg: None
    fallos = 0
    # Los casos que MUEREN escriben su ERROR en stderr, que es justo lo que se
    # esta probando: aqui es ruido esperado y taparia la lista de resultados.
    # Una excepcion inesperada si sale, porque sube y se imprime al salir.
    with contextlib.redirect_stderr(io.StringIO()):
        fallos = bloque_1_script(mod, fallos)
        fallos = bloque_2_codigo(mod, fallos)
        fallos = bloque_3_token(mod, fallos)
    print("\nTODO OK" if not fallos else f"\n{fallos} FALLOS")
    return 1 if fallos else 0


if __name__ == "__main__":
    sys.exit(main())
