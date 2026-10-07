#!/usr/bin/env python3
"""`almacen conectar` con un clon que NO comparte historia con el almacén.

    python3 tests/test_almacen_sincronizar.py

Repos git DE VERDAD, sin framework. Lo que se fija, y por qué estas cosas:

 1. Un dev nuevo nace con el repo de datos clonado de GitHub (la copia congelada) y el
    almacén tiene la historia compactada del 2026-10-03: no comparten ni un commit. Antes
    `conectar` lo dejaba en AVISO con exit 0 y el archivador no podía empujar nunca (medido el
    2026-10-07: ahead 758, behind 52). Ahora se REAJUSTA a origin/main si lo único local es
    regenerable, y se NIEGA si hay trabajo que no se puede rehacer.
 2. La trampa -fusionar historias no relacionadas- no se hace: HEAD acaba EXACTAMENTE en
    origin/main, sin un solo objeto de la historia vieja.
 3. Una divergencia CON historia común (un push que falló) no se decide aquí: problema.
 4. El camino de siempre (fast-forward) sigue igual.
"""

import importlib.util
import os
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def cargar():
    spec = importlib.util.spec_from_file_location("do_droplet", ROOT / "scripts" / "do_droplet.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


ENV = dict(os.environ, GIT_CONFIG_GLOBAL="/dev/null", GIT_CONFIG_NOSYSTEM="1",
           GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t", GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t")


def git(cwd: Path, *argv: str) -> str:
    p = subprocess.run(["git", "-C", str(cwd), *argv], capture_output=True, text=True, env=ENV)
    if p.returncode != 0:
        raise RuntimeError(f"git {' '.join(argv)} en {cwd}: {p.stderr}")
    return p.stdout.strip()


def repo_con(raiz: Path, nombre: str, commits: list[tuple[str, str]]) -> Path:
    """Un repo desnudo `nombre.git` con esos commits (fichero, contenido) en `main`."""
    bare = raiz / f"{nombre}.git"
    git(raiz, "init", "-q", "--bare", "-b", "main", str(bare))
    work = raiz / f"{nombre}-work"
    git(raiz, "init", "-q", "-b", "main", str(work))
    for fichero, contenido in commits:
        (work / fichero).parent.mkdir(parents=True, exist_ok=True)
        (work / fichero).write_text(contenido, encoding="utf-8")
        git(work, "add", "-A")
        git(work, "commit", "-q", "-m", f"{fichero}: {contenido[:12]}")
    git(work, "remote", "add", "origin", str(bare))
    git(work, "push", "-q", "-u", "origin", "main")
    return bare


def commit_local(clon: Path, fichero: str, contenido: str) -> None:
    (clon / fichero).parent.mkdir(parents=True, exist_ok=True)
    (clon / fichero).write_text(contenido, encoding="utf-8")
    git(clon, "add", "-A")
    git(clon, "commit", "-q", "-m", f"local: {fichero}")


def main() -> int:
    mod = cargar()
    fallos = 0

    def caso(nombre: str, ok: bool, detalle: str = "") -> None:
        nonlocal fallos
        fallos += not ok
        print(f"  {'ok   ' if ok else 'FALLO'} {nombre}{'  -> ' + detalle if detalle else ''}")

    def escenario_github(tmp: Path, extra_local=None) -> tuple[Path, Path]:
        """Como nace un dev: clon de GitHub (historia vieja) con origin apuntado al almacén."""
        github = repo_con(tmp, "github", [("datos.txt", "v1 en github"), ("datos.txt", "v2 en github")])
        almacen = repo_con(tmp, "almacen", [("datos.txt", "compactado el 2026-10-03")])
        clon = tmp / "clon"
        git(tmp, "clone", "-q", str(github), str(clon))
        if extra_local:
            commit_local(clon, *extra_local)
        # lo que hace `almacen conectar` antes del pull: github se queda como remoto, origin -> almacén
        git(clon, "remote", "add", "github", str(github))
        git(clon, "remote", "set-url", "origin", str(almacen))
        git(clon, "fetch", "-q", "origin")
        return clon, almacen

    # --- 1. el caso del 2026-10-07: clon prístino de GitHub, almacén compactado ----------
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        clon, almacen = escenario_github(tmp)
        estado, detalle = mod.sincronizar_clon_con_almacen(clon)
        caso("clon de GitHub + almacén compactado -> REAJUSTADO", estado == "reajustado", detalle)
        caso("...y HEAD es exactamente origin/main",
             git(clon, "rev-parse", "HEAD") == git(clon, "rev-parse", "origin/main"))
        caso("...sin arrastrar la historia vieja (un solo commit alcanzable)",
             git(clon, "rev-list", "--count", "HEAD") == "1")
        caso("...el remoto github sigue declarado (la historia vieja cuenta como guardada)",
             "github" in git(clon, "remote").split())

    # --- 2. con un commit local REGENERABLE (el archivo que el hook commiteó) -----------
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        clon, almacen = escenario_github(tmp, ("conversaciones/2026/10-octubre/x.jsonl.gz", "gz"))
        estado, detalle = mod.sincronizar_clon_con_almacen(clon)
        caso("commit local que sólo toca conversaciones/ -> se REAJUSTA igual (se regenera solo)",
             estado == "reajustado" and "conversaciones/" in detalle, detalle)
        caso("...HEAD == origin/main", git(clon, "rev-parse", "HEAD") == git(clon, "rev-parse", "origin/main"))

    # --- 3. con un commit local que NO se puede regenerar: se NIEGA ---------------------
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        clon, almacen = escenario_github(tmp, ("errores/2026/10.jsonl", "un error registrado a mano"))
        antes = git(clon, "rev-parse", "HEAD")
        estado, detalle = mod.sincronizar_clon_con_almacen(clon)
        caso("commit local NO regenerable -> PROBLEMA, no se tira", estado == "problema", detalle)
        caso("...y HEAD no se ha movido", git(clon, "rev-parse", "HEAD") == antes)
        caso("...el mensaje nombra el fichero en riesgo", "errores/2026/10.jsonl" in detalle)

    # --- 4. el camino de siempre: fast-forward -----------------------------------------
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        almacen = repo_con(tmp, "almacen", [("datos.txt", "uno")])
        clon = tmp / "clon"
        git(tmp, "clone", "-q", str(almacen), str(clon))
        work = tmp / "almacen-work"
        commit_local(work, "datos.txt", "dos")
        git(work, "push", "-q", "origin", "main")
        estado, detalle = mod.sincronizar_clon_con_almacen(clon)
        caso("historia común y nada local -> AL DÍA (fast-forward)", estado == "al-dia", detalle)
        caso("...HEAD == origin/main", git(clon, "rev-parse", "HEAD") == git(clon, "rev-parse", "origin/main"))

    # --- 5. divergencia CON historia común: un push que falló. No se decide aquí --------
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        almacen = repo_con(tmp, "almacen", [("datos.txt", "uno")])
        clon = tmp / "clon"
        git(tmp, "clone", "-q", str(almacen), str(clon))
        commit_local(clon, "errores/x.jsonl", "lo mío")
        work = tmp / "almacen-work"
        commit_local(work, "datos.txt", "lo de otro")
        git(work, "push", "-q", "origin", "main")
        git(clon, "fetch", "-q", "origin")
        antes = git(clon, "rev-parse", "HEAD")
        estado, detalle = mod.sincronizar_clon_con_almacen(clon)
        caso("divergencia con base común -> PROBLEMA (es un push que falló)",
             estado == "problema" and "rebase" in detalle, detalle)
        caso("...y HEAD no se ha movido", git(clon, "rev-parse", "HEAD") == antes)

    # --- 6. árbol sucio: ni pull ni reset ---------------------------------------------
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        clon, almacen = escenario_github(tmp)
        (clon / "datos.txt").write_text("a medias", encoding="utf-8")
        estado, detalle = mod.sincronizar_clon_con_almacen(clon)
        caso("cambios sin commitear -> SIN PULL, no se toca nada", estado == "sin-pull", detalle)
        caso("...el fichero a medias sigue", (clon / "datos.txt").read_text(encoding="utf-8") == "a medias")

    # --- 7. el cableado: conectar cuenta «problema» y el AVISO viejo ya no existe -------
    fuente = (ROOT / "scripts" / "do_droplet.py").read_text(encoding="utf-8")
    caso("almacen_conectar usa sincronizar_clon_con_almacen y cuenta los problemas",
         'estado, detalle = sincronizar_clon_con_almacen(clon)' in fuente
         and 'if estado == "problema":' in fuente)
    caso("el AVISO mudo de «no avanza en fast-forward» ya no está",
         "AVISO: no avanza en fast-forward" not in fuente)

    print(f"\n{'TODO OK' if not fallos else f'{fallos} FALLO(S)'}")
    return 1 if fallos else 0


if __name__ == "__main__":
    raise SystemExit(main())
