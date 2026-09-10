#!/usr/bin/env python3
"""La clave de la flota, los entornos, y la red que impide que un .env llegue a git.

    python3 tests/test_flota.py

Sin framework ni dependencias, como el resto.

Lo que se fija aqui, y por que cada cosa:

  - `keys --prune` NUNCA puede borrar la clave de la flota ni la de esta
    maquina. Es el unico comando destructivo de los nuevos, y borrar la de la
    flota dejaria a todas las maquinas nuevas sin acceso entre ellas.
  - `git_ignora()` tiene que distinguir TRES estados, no dos: ignorado, NO
    ignorado, y "esto no es un repo". Confundir el tercero con el primero
    escribiria secretos donde no hay quien los proteja.
  - dos entornos que escriban el mismo fichero tienen que PARAR el comando.
    `telegram-coordinator` y `telegram-launcher` son el mismo repo con otro bot,
    y aplicarlos juntos dejaria al mini con el bot del dev: un 409 de Telegram
    en el que nadie pensaria.
  - los scripts remotos no pueden llevar un `\\n` de Python donde el shell
    esperaba dos caracteres. Ese fallo no rompe la sintaxis: parte el script en
    dos lineas y se descubre en la maquina.
"""

import importlib.util
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
    mod.log = lambda *_a, **_k: None
    return mod


class Muerte(Exception):
    pass


def test_prune_protege(mod):
    """`keys --prune` no puede llevarse la clave de la flota. Nunca."""
    fallos = []
    borradas = []
    mod.api = lambda method, path, body=None: borradas.append(path)
    mod.confirmar = lambda _p: True

    claves = [
        {"id": 1, "name": "flota", "fingerprint": "aa", "public_key": "ssh-ed25519 AAAflota f"},
        {"id": 2, "name": "lanzador-dev", "fingerprint": "bb", "public_key": "ssh-ed25519 AAAd1 x"},
        {"id": 3, "name": "lanzador-dev", "fingerprint": "cc", "public_key": "ssh-ed25519 AAAd2 x"},
        {"id": 4, "name": "mi-laptop", "fingerprint": "dd", "public_key": "ssh-ed25519 AAAlap x"},
    ]

    # El patron encaja con TODO a proposito: aun asi, la de la flota se salva.
    mod._podar_claves(claves, "*", yes=True)
    if "/v2/account/keys/1" in borradas:
        fallos.append("borro la clave de la FLOTA con --prune '*'")
    if len(borradas) != 3:
        fallos.append(f"con '*' esperaba borrar 3 y borro {len(borradas)}")

    borradas.clear()
    mod._podar_claves(claves, "lanzador-*", yes=True)
    if sorted(borradas) != ["/v2/account/keys/2", "/v2/account/keys/3"]:
        fallos.append(f"'lanzador-*' borro lo que no debia: {borradas}")

    borradas.clear()
    mod._podar_claves(claves, "no-encaja-con-nada", yes=True)
    if borradas:
        fallos.append("borro algo con un patron que no encaja")
    return fallos


def test_prune_pide_confirmacion(mod):
    """Sin --yes y sin decir 'si', no se borra nada."""
    borradas = []
    mod.api = lambda method, path, body=None: borradas.append(path)
    mod.confirmar = lambda _p: False
    claves = [
        {"id": 2, "name": "lanzador-dev", "fingerprint": "bb",
         "public_key": "ssh-ed25519 AAAd1 x"},
    ]
    mod._podar_claves(claves, "lanzador-*", yes=False)
    return [] if not borradas else ["borro sin confirmacion"]


def test_ruta_publica(mod):
    """La .pub se CONCATENA; with_suffix se comeria una extension existente."""
    fallos = []
    casos = [
        ("/home/x/.ssh/do_flota", "/home/x/.ssh/do_flota.pub"),
        # El que rompe with_suffix: se quedaria en id_ed25519.pub y perderia .old
        ("/home/x/.ssh/id_ed25519.old", "/home/x/.ssh/id_ed25519.old.pub"),
    ]
    for entrada, espera in casos:
        sale = str(mod.ruta_publica(Path(entrada))).replace("\\", "/")
        if not sale.endswith(espera.lstrip("/")):
            fallos.append(f"{entrada} -> {sale}, esperaba terminar en {espera}")
    return fallos


def test_git_ignora(mod):
    """Tres estados, no dos: ignorado, NO ignorado, y no-es-un-repo."""
    fallos = []
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)

        sin_git = base / "sin-git"
        sin_git.mkdir()
        if mod.git_ignora(sin_git, ".env") is not None:
            fallos.append("un directorio sin git no devolvio None")

        repo = base / "repo"
        repo.mkdir()
        subprocess.run(["git", "init", "-q", str(repo)], capture_output=True)
        if not (repo / ".git").exists():
            return ["no pude crear un repo de prueba; git no esta disponible"]

        # Sin .gitignore: NO ignorado. Es el caso real de
        # claude-code-webapp-mobile el 2026-09-10, un repo publico.
        if mod.git_ignora(repo, ".env") is not False:
            fallos.append("sin .gitignore deberia decir NO ignorado")

        (repo / ".gitignore").write_text(".env\n", encoding="utf-8")
        if mod.git_ignora(repo, ".env") is not True:
            fallos.append("con .env en .gitignore deberia decir ignorado")
        # Y que no se pase de listo: .env.example SI tiene que estar en git.
        (repo / ".gitignore").write_text(".env\n.env.*\n!.env.example\n", encoding="utf-8")
        if mod.git_ignora(repo, ".env.example") is not False:
            fallos.append("el .env.example no deberia salir como ignorado")
    return fallos


def test_entornos_descriptores(mod):
    """Cada entorno declara a donde va cada variable y de donde sale."""
    fallos = []
    entornos = mod.all_entornos()
    if not entornos:
        return ["no hay ningun entorno declarado en entornos/"]
    llavero = {v["nombre"] for v in mod.cargar_llavero()}
    for ent in entornos:
        if not ent.get("dir"):
            fallos.append(f"{ent['name']}: sin 'dir'")
        for var in ent["variables"]:
            if not var.get("desde"):
                fallos.append(f"{ent['name']}/{var['nombre']}: sin 'desde'")
            if not var.get("porque"):
                fallos.append(f"{ent['name']}/{var['nombre']}: no dice para que es")
            # Una obligatoria cuyo origen no este en el llavero no llegara nunca:
            # el .env se genera del llavero, no del aire.
            if var["obligatoria"] and var["desde"] not in llavero:
                fallos.append(
                    f"{ent['name']}/{var['nombre']} es obligatoria y su origen "
                    f"'{var['desde']}' no esta en el llavero")
    return fallos


def test_puente_tailscale(mod):
    """El caso que costo el error: CWEB_TS_AUTHKEY aqui -> TS_AUTHKEY alli.

    El nombre de destino lo declara quien CONSUME (scripts/tailscale-unir.mjs lee
    TS_AUTHKEY), no quien transporta. El 2026-09-10 se puso como
    TAILSCALE_AUTHKEY y no llegaba a ninguna parte: el secreto se quedaba en el
    llavero sin bajar al servicio, y la app arrancaba sin unirse al tailnet y sin
    un solo error.
    """
    ent = mod.load_entorno("claude-code-webapp-mobile")
    var = next((v for v in ent["variables"] if v["nombre"] == "TS_AUTHKEY"), None)
    if not var:
        return ["el entorno de la app movil ya no declara TS_AUTHKEY"]
    fallos = []
    if var["desde"] != "CWEB_TS_AUTHKEY":
        fallos.append(f"el origen deberia ser CWEB_TS_AUTHKEY y es {var['desde']}")
    # Y el prefijo tiene que cuadrar con el env_prefix del servicio, o el puente
    # de provision y el de `entornos aplicar` escribirian cosas distintas.
    svc = mod.load_service("claude-web")
    if not var["desde"].startswith(svc["env_prefix"]):
        fallos.append(
            f"'{var['desde']}' no empieza por el env_prefix del servicio "
            f"('{svc['env_prefix']}'): los dos caminos divergirian")
    return fallos


def test_colision_de_entornos(mod):
    """Dos entornos al mismo fichero tienen que PARAR el comando."""
    def _die(msg):
        raise Muerte(msg)
    mod.die = _die

    import argparse
    mismos = [e["name"] for e in mod.all_entornos()
              if e["dir"] == "telegram-coordinator"]
    if len(mismos) < 2:
        return ["ya no hay dos entornos al mismo fichero; el test no prueba nada"]
    args = argparse.Namespace(entorno=[], accion="aplicar")
    try:
        mod._entornos_aplicar(Path(tempfile.gettempdir()), args)
        return ["dos entornos al mismo fichero NO pararon el comando"]
    except Muerte as exc:
        falta = [n for n in mismos if n not in str(exc)]
        return [f"el error no nombra {falta}"] if falta else []


def test_escapes_de_scripts_remotos(mod):
    """Ningun script remoto puede llevar un salto de linea donde iba `\\n`.

    No rompe la sintaxis de Python: parte el script del droplet en dos lineas, y
    eso solo se descubre ya en la maquina.
    """
    fuente = (ROOT / "scripts" / "do_droplet.py").read_text(encoding="utf-8")
    fallos = []
    for numero, linea in enumerate(fuente.splitlines(), 1):
        pelada = linea.strip()
        if "printf" not in pelada or not pelada.startswith(("'", 'f"', '"')):
            continue
        # En el codigo fuente tiene que verse `\\n` (dos barras) para que el
        # shell reciba `\n`. Una sola barra ya la consumio Python.
        if "%s\\n" in pelada and "%s\\\\n" not in pelada:
            fallos.append(f"linea {numero}: printf con \\n de Python, no del shell")
    return fallos


def test_ejecutores(mod):
    """Los ejecutores nuevos existen, son validos y respetan las convenciones."""
    import json
    fallos = []
    carpeta = ROOT / "telegram" / "executors"
    nuevos = {"remoto", "flota", "llavero"}
    encontrados = set()
    for path in sorted(carpeta.glob("*.json")):
        datos = json.loads(path.read_text(encoding="utf-8"))
        encontrados.add(datos.get("name", path.stem))
        for campo in ("name", "command", "descripcion"):
            if not datos.get(campo):
                fallos.append(f"{path.name}: falta '{campo}'")
        # Convencion del repo: el coordinador ya pone el cwd en la raiz del repo.
        if "cd " in datos.get("command", ""):
            fallos.append(f"{path.name}: lleva un 'cd' y no debe")
    for falta in nuevos - encontrados:
        fallos.append(f"falta el ejecutor '{falta}': el comando existe y no se puede pedir desde el movil")
    return fallos


def test_comandos_registrados(mod):
    """Los comandos nuevos estan en el parser: si no, no existen para nadie."""
    import argparse
    fallos = []
    esperados = {
        "clave-flota": "cmd_clave_flota",
        "autorizar-flota": "cmd_autorizar_flota",
        "remoto": "cmd_remoto",
        "flota": "cmd_flota",
        "entornos": "cmd_entornos",
        "llavero": "cmd_llavero",
    }
    fuente = (ROOT / "scripts" / "do_droplet.py").read_text(encoding="utf-8")
    for comando, funcion in esperados.items():
        if f'"{comando}"' not in fuente:
            fallos.append(f"'{comando}' no aparece en el parser")
        if f"def {funcion}(" not in fuente:
            fallos.append(f"falta la funcion {funcion}")
        if f"func={funcion}" not in fuente:
            fallos.append(f"'{comando}' no esta enganchado a {funcion}")
    return fallos


def main():
    pruebas = [
        ("prune nunca borra la clave de la flota", test_prune_protege),
        ("prune pide confirmacion", test_prune_pide_confirmacion),
        ("ruta_publica concatena, no sustituye", test_ruta_publica),
        ("git_ignora distingue tres estados", test_git_ignora),
        ("los entornos declaran origen y destino", test_entornos_descriptores),
        ("el puente CWEB_TS_AUTHKEY -> TS_AUTHKEY", test_puente_tailscale),
        ("dos entornos al mismo fichero paran", test_colision_de_entornos),
        ("los scripts remotos no llevan \\n de Python", test_escapes_de_scripts_remotos),
        ("los ejecutores del bot estan y son validos", test_ejecutores),
        ("los comandos nuevos estan registrados", test_comandos_registrados),
    ]
    total = 0
    for nombre, prueba in pruebas:
        mod = cargar()  # modulo limpio por prueba: varias parchean die/api
        fallos = prueba(mod)
        total += len(fallos)
        print(f"  {'ok   ' if not fallos else 'FALLO'} {nombre}")
        for fallo in fallos:
            print(f"          {fallo}")
    print(f"\n{len(pruebas)} pruebas, {total} fallo(s)")
    return 1 if total else 0


if __name__ == "__main__":
    sys.exit(main())
