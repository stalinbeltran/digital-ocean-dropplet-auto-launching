#!/usr/bin/env python3
"""Que `launch` anuncie la direccion del servicio, y que CALLE cuando no la sabe.

    python3 tests/test_url_servicio.py

Sin framework y sin dependencias a proposito: este repo corre con el python3
pelado del sistema, igual que `web_app.py preparar`. Un test que pida instalar
algo no se ejecuta en la maquina donde importa.

Lo que se fija aqui es el CONTRATO de `url_de_servicio`, que es la parte
facil de romper sin que se note: la ultima linea no vacia de stdout, solo si
el comando salio bien y eso parece una direccion. Las tres condiciones son
necesarias, y cada una tiene su caso abajo.

⚠ Lo que este test NO cubre: que el comando remoto funcione de verdad. Eso
depende de correr como el usuario de desarrollo (§ url_de_servicio) y solo se
puede comprobar contra una maquina. Medido a mano el 2026-08-30 en un dev con
foveal-vision-web: como `deploy` da la URL y exit 0; como root da "No hay
token todavia" y exit 1, porque el token vive en ~/.config de deploy.
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


URL = "http://1.2.3.4:8010/?t=abc"

# nombre, (exit, stdout, stderr), se espera direccion?
CASOS = [
    ("lo normal",              (0, URL + "\n", ""),                       True),
    # El comando corre con shell de LOGIN para ver dev-secrets.env, asi que un
    # .bashrc que salude escribe en stdout por delante de la direccion.
    ("saludo del .bashrc",     (0, "Bienvenido\n" + URL + "\n", ""),      True),
    # Y por esto hace falta stderr aparte: mezclado, este aviso seria la ultima
    # linea y se publicaria como si fuera la direccion.
    ("aviso de ssh en stderr", (0, URL + "\n", "Warning: added host\n"),  True),
    ("sin token (exit 1)",     (1, "No hay token todavia.\n", ""),        False),
    # El caso que obliga a mirar el codigo de salida, y el unico que lo hace: un
    # comando que imprime algo con pinta de direccion Y ADEMAS falla. Sin este
    # caso, quitar el `code == 0` no rompia ningun test (comprobado el
    # 2026-08-30 mutando el codigo a proposito). Si el servicio dice que no
    # salio bien, su salida no se publica por muy buena pinta que tenga.
    ("exit 1 aunque imprimio",  (1, URL + "\n", ""),                      False),
    ("no existe el repo",      (1, "", "cd: no such file or directory\n"), False),
    ("no imprimio nada",       (0, "", ""),                               False),
    # Exit 0 y una linea: lo unico que lo descarta es que no parezca direccion.
    # Publicarla mandaria al usuario a una pagina que no carga.
    ("exit 0 pero no es URL",  (0, "listo\n", ""),                        False),
]


def main() -> int:
    mod = cargar()
    svc = mod.load_service("foveal-vision-web")
    if not svc.get("url"):
        print("FALLO  el descriptor foveal-vision-web perdio su campo 'url'")
        return 1

    fallos = 0
    for nombre, respuesta, espera_url in CASOS:
        mod.run_remote_split = lambda ip, port, s, _r=respuesta: _r
        direccion, aviso = mod.url_de_servicio(svc, "dev", "1.2.3.4", 22, "deploy")
        # Exactamente uno de los dos trae texto: un aviso vacio se leeria como
        # "no hay nada que abrir" y una direccion vacia se imprimiria en blanco.
        ok = bool(direccion) == espera_url and bool(aviso) == (not espera_url)
        if ok and not espera_url:
            # Un aviso que no dice como arreglarlo obliga a leer el codigo.
            ok = "do_droplet.py ssh" in aviso
        fallos += not ok
        print(f"  {'ok   ' if ok else 'FALLO'} {nombre:24} -> "
              f"{direccion or aviso.splitlines()[0]}")

    print(f"\n{len(CASOS) - fallos}/{len(CASOS)} pasan")
    return 1 if fallos else 0


if __name__ == "__main__":
    sys.exit(main())
