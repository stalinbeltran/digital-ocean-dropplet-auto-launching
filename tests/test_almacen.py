#!/usr/bin/env python3
"""El almacén: que NADIE pueda borrar en el remoto git del volumen, y que lo declarado
sea lo que se instala.

    python3 tests/test_almacen.py

Sin framework y sin dependencias, como el resto del repo. Necesita `git` en el PATH,
que es lo único que el almacén necesita también.

QUÉ SE FIJA AQUÍ, y por qué estas cosas y no «que funcione»:

 1. La regla «nadie borra» es la razón entera de este diseño (dueño, 2026-10-01). Se
    prueba contra un repo desnudo DE VERDAD preparado con la MISMA función que usa
    `almacen instalar` en el mini: un push normal entra, un `--delete` se rechaza, un
    `--force` se rechaza, y el hook deja rastro de lo que entró (R12). Si alguien
    afloja la config o el hook, esto falla antes de que llegue al volumen.
 2. El alias de ~/.ssh/config se reescribe sin pisar el resto del fichero y sin
    duplicarse: rehacer el mini cambia la IP, y esto corre en cada máquina cada vez.
 3. Lo que `launch mini` monta y `post` instala es DATO en types/mini.json: el volumen
    declarado, los repos, y el orden instalar -> conectar. Y el dev conecta en su post.
 4. Terminado = invocable desde Telegram: el ejecutor `almacen` existe y llama al
    subcomando.
"""

import importlib.util
import json
import os
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


def git(cwd: Path, *argv: str) -> tuple[int, str]:
    env = dict(os.environ, GIT_CONFIG_GLOBAL="/dev/null", GIT_CONFIG_NOSYSTEM="1",
               GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t", GIT_COMMITTER_NAME="t",
               GIT_COMMITTER_EMAIL="t@t")
    p = subprocess.run(["git", "-C", str(cwd), *argv], capture_output=True, text=True, env=env)
    return p.returncode, (p.stdout + p.stderr)


def main() -> int:
    mod = cargar()
    fallos = 0

    def caso(nombre: str, ok: bool, detalle: str = "") -> None:
        nonlocal fallos
        fallos += not ok
        print(f"  {'ok   ' if ok else 'FALLO'} {nombre}{'  -> ' + detalle if detalle else ''}")

    # --- 1. nadie borra, contra un repo desnudo de verdad ---------------------
    with tempfile.TemporaryDirectory() as tmp:
        t = Path(tmp)
        desnudo, log = t / "datos.git", t / "pushes.log"
        git(t, "init", "-q", "--bare", str(desnudo))
        mod.preparar_repo_desnudo(desnudo, log)
        clon = t / "clon"
        git(t, "clone", "-q", str(desnudo), str(clon))
        (clon / "a.txt").write_text("1\n")
        git(clon, "add", "a.txt")
        git(clon, "commit", "-q", "-m", "uno")
        (clon / "a.txt").write_text("2\n")
        git(clon, "commit", "-q", "-am", "dos")
        code, out = git(clon, "push", "-q", "origin", "HEAD:refs/heads/main")
        caso("un push normal entra", code == 0, out.strip()[-120:])
        code, out = git(clon, "push", "origin", "HEAD:refs/heads/rama")
        caso("una rama nueva entra", code == 0, out.strip()[-120:])

        code, out = git(clon, "push", "origin", "--delete", "rama")
        caso("borrar una rama se RECHAZA", code != 0 and "NO se puede borrar" in out,
             "" if code != 0 else "¡se borró!")
        code, out = git(desnudo, "rev-parse", "--verify", "-q", "refs/heads/rama")
        caso("...y la rama sigue ahí", code == 0)

        git(clon, "reset", "-q", "--hard", "HEAD~1")
        (clon / "b.txt").write_text("x\n")
        git(clon, "add", "b.txt")
        git(clon, "commit", "-q", "-m", "otra historia")
        code, out = git(clon, "push", "--force", "origin", "HEAD:refs/heads/main")
        caso("reescribir historia (--force) se RECHAZA", code != 0 and "NO es fast-forward" in out,
             "" if code != 0 else "¡entró!")
        code, out = git(desnudo, "log", "--format=%s", "-1", "refs/heads/main")
        caso("...y main conserva lo que tenía", out.strip() == "dos", out.strip())

        lineas = log.read_text().splitlines() if log.exists() else []
        caso("el hook apunta cada push que entra (R12)", len(lineas) == 2, f"{len(lineas)} líneas")
        caso("la config lleva denyDeletes y denyNonFastForwards",
             git(desnudo, "config", "receive.denyDeletes")[1].strip() == "true"
             and git(desnudo, "config", "receive.denyNonFastForwards")[1].strip() == "true")

    # --- 1 bis. se reempaqueta solo: la historia no se guarda dos veces -------
    # (2026-10-03: el volumen se llenó al 100 % con packs gemelos que nadie juntaba)
    with tempfile.TemporaryDirectory() as tmp:
        t = Path(tmp)
        desnudo, log = t / "datos.git", t / "pushes.log"
        git(t, "init", "-q", "--bare", str(desnudo))
        mod.preparar_repo_desnudo(desnudo, log)
        clon = t / "clon"
        git(t, "clone", "-q", str(desnudo), str(clon))
        maximo = 0
        for i in range(7):
            (clon / f"f{i}.bin").write_bytes(os.urandom(50_000))
            git(clon, "add", ".")
            git(clon, "commit", "-q", "-m", f"c{i}")
            git(clon, "push", "-q", "origin", "HEAD:refs/heads/main")
            maximo = max(maximo, len(list((desnudo / "objects" / "pack").glob("*.pack"))))
        caso("siete pushes nunca dejan más de 4 packs (gc --auto tras el push)", maximo <= 4,
             f"máximo {maximo}")
        sueltos = [f for d in (desnudo / "objects").iterdir() if len(d.name) == 2 for f in d.iterdir()]
        caso("...y ningún push queda como objetos sueltos (unpackLimit 1)", not sueltos,
             f"{len(sueltos)} sueltos")
        code, out = git(desnudo, "log", "--format=%s", "-1", "refs/heads/main")
        caso("...y no se pierde nada al reempaquetar", out.strip() == "c6", out.strip())

    # --- 2. el alias de ssh, idempotente y sin pisar nada --------------------
    with tempfile.TemporaryDirectory() as tmp:
        config = Path(tmp) / "config"
        config.write_text("Host otra\n  HostName 10.0.0.1\n")
        clave = Path(tmp) / "do_flota"
        mod.escribir_alias_ssh("1.2.3.4", config, clave)
        mod.escribir_alias_ssh("5.6.7.8", config, clave)
        texto = config.read_text()
        caso("el alias lleva la IP nueva y sólo una vez",
             texto.count("Host almacen") == 1 and "HostName 5.6.7.8" in texto
             and "HostName 1.2.3.4" not in texto)
        caso("el resto del fichero no se toca", "Host otra\n  HostName 10.0.0.1" in texto)
        caso("el usuario es `datos` y la clave es la de flota",
             "User datos" in texto and f"IdentityFile {clave}" in texto)
        caso("permisos 600", oct(config.stat().st_mode & 0o777) == "0o600")
        caso("la clave de host se recuerda por el ALIAS, no por la IP (HostKeyAlias)",
             "HostKeyAlias almacen" in texto)

    # --- 2 bis. instalar se NIEGA si el volumen no está montado (R2) -------------
    # Sin esto, el servicio git se instalaría en el disco del droplet: justo donde se
    # pierde, y con todos los indicadores en verde.
    class Muerte(Exception):
        pass

    def _die(msg):
        raise Muerte(msg)

    die_real, dentro_real, ismount_real = mod.die, mod.dentro_del_droplet, mod.os.path.ismount
    mod.die = _die
    mod.dentro_del_droplet = lambda *a, **k: Path("/nada")
    mod.os.path.ismount = lambda p: False
    try:
        try:
            mod.almacen_instalar({"volumen": "datos", "monte": "/mnt/datos", "repos": ["x"],
                                  "apps": {}, "github": "y"}, seco=False)
            caso("instalar sin volumen montado se NIEGA", False, "no murió")
        except Muerte as e:
            caso("instalar sin volumen montado se NIEGA", "no está montado" in str(e))
    finally:
        mod.die, mod.dentro_del_droplet, mod.os.path.ismount = die_real, dentro_real, ismount_real

    # --- 3. lo declarado en los tipos --------------------------------------
    mini = json.loads((ROOT / "types" / "mini.json").read_text(encoding="utf-8"))
    dev = json.loads((ROOT / "types" / "dev.json").read_text(encoding="utf-8"))
    caso("types/mini.json declara el volumen", mini.get("volume") == "datos")
    caso("...y el repo de datos como espejo", "foveal-vision-data" in (mini.get("almacen") or {}).get("repos", []))
    post = mini.get("post") or []
    i_inst = next((i for i, c in enumerate(post) if "almacen instalar" in c), -1)
    i_con = next((i for i, c in enumerate(post) if "almacen conectar" in c), -1)
    caso("el post del mini instala y DESPUÉS conecta", 0 <= i_inst < i_con)
    caso("el post del dev conecta", any("almacen conectar" in c for c in (dev.get("post") or [])))
    # El invariante que dejó al descubierto `bench-control` (retirado el 2026-10-01):
    # clonaba el repo de datos sin `almacen conectar`, así que habría empujado a la
    # copia congelada de GitHub sin avisar. Estaba escrito como nota en el CLAUDE.md
    # del coordinador; una nota no es un freno, y esto sí.
    sin_conectar = []
    for t in sorted((ROOT / "types").glob("*.json")):
        d = json.loads(t.read_text(encoding="utf-8"))
        clona = any("foveal-vision-data" in r for r in (d.get("repos") or []))
        if clona and not any("almacen conectar" in c for c in (d.get("post") or [])):
            sin_conectar.append(t.stem)
    caso("todo tipo que clona el repo de datos se conecta al almacén",
         not sin_conectar, ", ".join(sin_conectar))
    # Y desde el 2026-10-07, todo tipo que corra el coordinador RESTAURA el historial de la web
    # de lectura en su post, DESPUES de conectar (la restauracion se niega si el clon no esta
    # conectado al almacen). Sin esto, un dev nuevo nace con la web vacia y nadie se entera.
    sin_restaurar = []
    for t in sorted((ROOT / "types").glob("*.json")):
        d = json.loads(t.read_text(encoding="utf-8"))
        if "telegram-coordinator" not in (d.get("services") or []):
            continue
        post_t = d.get("post") or []
        i_c = next((i for i, c in enumerate(post_t) if "almacen conectar" in c), -1)
        i_r = next((i for i, c in enumerate(post_t) if "estado-por-tema.mjs --restaurar" in c), -1)
        if not (0 <= i_c < i_r):
            sin_restaurar.append(t.stem)
    caso("todo tipo con el coordinador restaura el historial en su post, después de conectar",
         not sin_restaurar, ", ".join(sin_restaurar))
    # Desde el 2026-10-07 la web de lectura vive en el MINI, en modo remoto (una sola app,
    # visible aunque el dev no exista). Si vuelve al dev, habría dos apps y la del dev muere con él.
    cweb = json.loads((ROOT / "services" / "claude-web.json").read_text(encoding="utf-8"))
    caso("claude-web va en el mini y NO en el dev",
         "claude-web" in (mini.get("services") or []) and "claude-web" not in (dev.get("services") or []))
    caso("...y se instala en modo remoto", "--remoto" in cweb.get("install", ""), cweb.get("install", ""))
    alm = mod.almacen_declarado()
    caso("almacen_declarado() lee el tipo", alm["monte"] == "/mnt/datos" and alm["repos"])

    fuente = (ROOT / "scripts" / "do_droplet.py").read_text(encoding="utf-8")
    caso("launch tiene --sin-volumen (el staging de un mini2 sigue siendo posible)",
         '"--sin-volumen"' in fuente and 'getattr(args, "sin_volumen"' in fuente)
    caso("volume sabe crecer (resize), porque nadie borra y 1 GB se llena",
         '"resize", "destroy"' in fuente and 'accion == "resize"' in fuente)

    # --- 4. invocable desde Telegram ----------------------------------------
    ej = ROOT / "telegram" / "executors" / "almacen.json"
    caso("ejecutor almacen.json existe", ej.is_file())
    if ej.is_file():
        d = json.loads(ej.read_text(encoding="utf-8"))
        caso("...y llama al subcomando", "do_droplet.py almacen" in d.get("command", ""))

    # --- 5. el script de instalación sale de los mismos objetos --------------
    script = mod.script_instalar_almacen(alm, "deploy", Path("/home/deploy/.ssh/do_flota.pub"))
    caso("el script instala el hook con la ruta del log del volumen",
         "/mnt/datos/log/pushes.log" in script and "NO se puede borrar" in script)
    caso("el script crea el usuario con git-shell y clave restringida",
         "git-shell" in script and 'printf "restrict ' in script)
    caso("el script no vuelve a espejar si el repo ya está",
         'if [ ! -d "$R" ]' in script)
    # Medido el 2026-10-01 al rehacer el mini: `git config` como root sobre un repo que ya
    # era de `datos` moría con «not in a git directory» y la demo no se enlazaba al volumen.
    caso("toda orden git sobre el repo va como `datos`, nunca como root",
         'git -C "$R" config' not in script.replace('sudo -u "$U" -H git -C "$R" config', '')
         and 'sudo -u "$U" -H git -C "$R" config' in script)
    # Con una app de pega: desde el 2026-10-07 el mini no declara ninguna (sispla-demo se
    # retiró), y el mecanismo tiene que seguir comprobándose para la siguiente.
    estado = mod.script_estado_almacen(dict(alm, apps={"una-app": "datos"}))
    caso("estado comprueba que cada app apunta al volumen",
         "NO apunta al volumen" in estado and "una-app" in estado)
    caso("sin apps declaradas, estado e instalar siguen generándose",
         bool(mod.script_estado_almacen(dict(alm, apps={})))
         and bool(mod.script_instalar_almacen(dict(alm, apps={}), "deploy",
                                              Path("/home/deploy/.ssh/do_flota.pub"))))

    print(f"\n{'TODO OK' if not fallos else str(fallos) + ' FALLO(S)'}")
    return 1 if fallos else 0


if __name__ == "__main__":
    sys.exit(main())
