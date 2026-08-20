#!/usr/bin/env python3
"""Datasets que tienen que llegar a cualquier maquina, de cualquier proveedor.

El problema que resuelve: los datos con los que se entrena y se mide **no estan
en el repo del proyecto** -- estan gitignoreados porque son miles de ficheros
binarios. Asi que una maquina recien creada clona el codigo y se queda sin el
dato, y eso no se ve hasta que el trabajo ya esta pagado y fallando. Con Vast.ai
ademas no vale la solucion de DigitalOcean (un volumen de bloques): no se puede
conectar un volumen de DO a una maquina de otro proveedor.

La forma de esto es la misma que la de `types/`, `services/` y `benchmarks/`:
un JSON por dataset en `datasets/`, que es **dato y no codigo**. Anadir un
dataset es escribir un fichero, no tocar este script.

    python scripts/dataset.py list
    python scripts/dataset.py pack dirty-1000-80px
    python scripts/dataset.py fetch dirty-1000-80px
    python scripts/dataset.py check dirty-1000-80px

Cada dataset declara VARIAS fuentes y se prueban en orden, porque ninguna sirve
para todos los tamanos:

- `repo`  -- un tar.gz commiteado en este repositorio. Llega con `git clone`, sin
             red, sin credenciales y sin infraestructura, a cualquier proveedor.
             Es lo mejor hasta unas decenas de MB; por encima, engorda el repo
             para siempre y hay que pasar a `url`.
- `url`   -- una descarga publica (release de GitHub, S3, lo que sea). Escala a
             gigabytes y la maquina se lo baja ella sola, sin pasar por tu
             conexion. A cambio hay que publicarlo y mantenerlo vivo.
- `local` -- una copia en la maquina que lo tiene. Es la red de seguridad para
             cuando aun no se ha publicado nada, y la forma de FABRICAR las otras
             dos con `pack`.

**Todo se verifica con sha256.** Un dataset a medias o cambiado da numeros con
el mismo aspecto y otro significado, que es peor que no dar ninguno.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
import tarfile
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATASET_DIR = ROOT / "datasets"
BLOB_DIR = DATASET_DIR / "blobs"
# Donde se dejan los datasets ya desempaquetados cuando no se dice otra cosa.
# Coincide con lo que `provision` deja en un droplet, para que en la maquina de
# control no haya nada que configurar.
CAMPOS = ("descripcion", "destino", "fuentes")


def die(msg: str) -> "NoReturn":  # type: ignore[valid-type]
    print(f"\nERROR: {msg}\n", file=sys.stderr)
    raise SystemExit(1)


def log(msg: str) -> None:
    print(msg, flush=True)


def force_utf8_output() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except (AttributeError, OSError):
            pass


def load_env() -> None:
    """Carga .env sin dependencias. Repetido a proposito: cada script corre suelto."""
    env_file = ROOT / ".env"
    if not env_file.exists():
        return
    for raw in env_file.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def base_trabajo() -> Path:
    """Raiz bajo la que se desempaquetan los datasets y viven los repos.

    `$BENCH_SRC`, igual que en vast_instance.py: ~/src por defecto (donde
    provision deja los repos), otra cosa en la laptop. Se comprueba vacio y no
    con setdefault porque .env.example trae la linea puesta y sin valor.
    """
    if not os.environ.get("BENCH_SRC"):
        os.environ["BENCH_SRC"] = str(Path.home() / "src")
    return Path(os.environ["BENCH_SRC"]).expanduser()


def expandir(ruta: str) -> Path:
    base_trabajo()  # asegura BENCH_SRC
    p = Path(os.path.expandvars(ruta)).expanduser()
    return p if p.is_absolute() else (ROOT / p)


# ------------------------------------------------------------------ descriptores


def load_dataset(name: str) -> dict:
    path = DATASET_DIR / f"{name}.json"
    if not path.exists():
        hay = ", ".join(d["name"] for d in all_datasets()) or "(ninguno)"
        die(f"No existe el dataset '{name}'. Los declarados: {hay}")
    try:
        datos = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        die(f"{path.name} no es JSON valido: {exc}")
    faltan = [c for c in CAMPOS if c not in datos]
    if faltan:
        die(f"A {path.name} le faltan campos obligatorios: {', '.join(faltan)}")
    datos["name"] = name
    return datos


def all_datasets() -> list[dict]:
    salida = []
    if not DATASET_DIR.exists():
        return salida
    for path in sorted(DATASET_DIR.glob("*.json")):
        try:
            datos = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        datos["name"] = path.stem
        salida.append(datos)
    return salida


def sha256_de(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for trozo in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(trozo)
    return h.hexdigest()


def blob_de(ds: dict) -> Path:
    """Ruta del tar.gz dentro del repo, la declare o no explicitamente."""
    for f in ds["fuentes"]:
        if f.get("tipo") == "repo":
            return expandir(f["ruta"])
    return BLOB_DIR / f"{ds['name']}.tar.gz"


def destino_de(ds: dict, dest: str = "") -> Path:
    if dest:
        return Path(dest).expanduser()
    return base_trabajo() / ds["destino"]


# ----------------------------------------------------------------- empaquetar


def cmd_pack(args: argparse.Namespace) -> None:
    """Fabrica el tar.gz a partir de la copia local, y dice su sha256.

    Es el paso que convierte "lo tengo en mi maquina" en algo que se puede
    commitear o publicar. Imprime el sha256 para que se pegue en el descriptor:
    a mano y no automatico a proposito, porque cambiar el checksum es declarar
    que el dato cambio, y eso invalida las medidas anteriores. Que cueste un
    copiar y pegar es la idea.
    """
    ds = load_dataset(args.name)
    origen = None
    for f in ds["fuentes"]:
        if f.get("tipo") == "local":
            candidata = expandir(f["ruta"])
            if candidata.is_dir():
                origen = candidata
                break
    if args.origen:
        origen = Path(args.origen).expanduser()
    if not origen or not origen.is_dir():
        die(
            f"No encuentro una copia local de '{args.name}' con la que empaquetar.\n"
            f"  Pasa --origen <directorio>, o arregla la fuente 'local' del "
            f"descriptor.\n"
            f"  Como se regenera desde cero: {ds.get('regenerar', '(sin documentar)')}"
        )

    salida = Path(args.out).expanduser() if args.out else blob_de(ds)
    salida.parent.mkdir(parents=True, exist_ok=True)
    log(f"Empaquetando {origen} -> {salida}")

    ficheros = 0
    # Ordenado y con metadatos neutros: si no, dos empaquetados del MISMO dato
    # dan sha256 distintos (orden del sistema de ficheros, mtime, uid) y el
    # checksum deja de significar "es el mismo dato" para significar "lo hizo la
    # misma maquina el mismo dia", que no sirve de nada.
    def normalizar(info: tarfile.TarInfo) -> tarfile.TarInfo:
        info.uid = info.gid = 0
        info.uname = info.gname = ""
        info.mtime = 0
        return info

    tmp = Path(tempfile.mkdtemp(prefix="pack-")) / "d.tar"
    with tarfile.open(tmp, "w") as tar:
        for p in sorted(origen.rglob("*"), key=lambda x: str(x).replace("\\", "/")):
            if p.is_file():
                ficheros += 1
            tar.add(str(p), arcname=str(p.relative_to(origen)).replace("\\", "/"),
                    recursive=False, filter=normalizar)
    import gzip

    with tmp.open("rb") as origen_fh, gzip.GzipFile(
        filename="", mode="wb", fileobj=salida.open("wb"), mtime=0
    ) as gz:
        shutil.copyfileobj(origen_fh, gz)
    shutil.rmtree(tmp.parent, ignore_errors=True)

    suma = sha256_de(salida)
    tam = salida.stat().st_size
    log(f"  {ficheros} ficheros, {tam / 1e6:.1f} MB")
    log(f"  sha256: {suma}")
    if ds.get("sha256") == suma:
        log("  Coincide con el descriptor: el dato no ha cambiado.")
    else:
        log("\nPega esto en " + str((DATASET_DIR / f"{args.name}.json").name) + ":")
        log(f'  "sha256": "{suma}",')
        log(f'  "bytes": {tam},')
        log(f'  "ficheros": {ficheros}')
        if ds.get("sha256"):
            log(
                "\n  OJO: el descriptor traia otro sha256. Si el dato ha cambiado a\n"
                "  proposito, las medidas guardadas con el anterior YA NO SON\n"
                "  COMPARABLES con las nuevas."
            )


# ------------------------------------------------------------------- traer


def descargar(url: str, destino: Path) -> None:
    log(f"  descargando {url}")
    with urllib.request.urlopen(url, timeout=120) as resp, destino.open("wb") as fh:
        shutil.copyfileobj(resp, fh)


def resolver_blob(ds: dict, cache: Path | None = None) -> Path:
    """Consigue el tar.gz del dataset probando las fuentes en orden.

    El orden no es arbitrario: primero lo que ya esta en disco (gratis y sin
    red), luego lo que se descarga, y por ultimo fabricarlo de una copia local.
    Asi una maquina sin credenciales ni internet sigue funcionando si el blob
    viajo en el repo.
    """
    for f in ds["fuentes"]:
        tipo = f.get("tipo")
        if tipo == "repo":
            p = expandir(f["ruta"])
            if p.is_file():
                log(f"  fuente: repo ({p.name})")
                return p
        elif tipo == "url":
            destino = (cache or Path(tempfile.mkdtemp(prefix="ds-"))) / f"{ds['name']}.tar.gz"
            if destino.is_file() and ds.get("sha256") == sha256_de(destino):
                log("  fuente: cache ya descargada")
                return destino
            try:
                destino.parent.mkdir(parents=True, exist_ok=True)
                descargar(f["url"], destino)
                return destino
            except (urllib.error.URLError, OSError) as exc:
                log(f"  la url fallo ({exc}); pruebo la siguiente fuente")
        elif tipo == "local":
            p = expandir(f["ruta"])
            if p.is_dir():
                log(f"  fuente: copia local ({p}); empaquetando al vuelo")
                tmp = Path(tempfile.mkdtemp(prefix="ds-")) / f"{ds['name']}.tar.gz"
                with tarfile.open(tmp, "w:gz") as tar:
                    tar.add(str(p), arcname=".")
                return tmp
    die(
        f"Ninguna fuente de '{ds['name']}' esta disponible en esta maquina.\n"
        f"  Fuentes declaradas: "
        + ", ".join(f.get("tipo", "?") for f in ds["fuentes"])
        + f"\n  Como se regenera desde cero: {ds.get('regenerar', '(sin documentar)')}"
    )


def verificar(ds: dict, blob: Path) -> None:
    esperado = ds.get("sha256") or ""
    if not esperado:
        log("  AVISO: el descriptor no declara sha256, no se puede verificar.")
        return
    real = sha256_de(blob)
    if real != esperado:
        die(
            f"El dataset '{ds['name']}' no cuadra.\n"
            f"  esperado: {esperado}\n"
            f"  obtenido: {real}\n"
            "  No se desempaqueta: medir con un dato distinto da un numero con el\n"
            "  mismo aspecto y otro significado, que es peor que no medir."
        )
    log("  sha256 correcto")


def cmd_fetch(args: argparse.Namespace) -> None:
    ds = load_dataset(args.name)
    destino = destino_de(ds, args.dest)
    if destino.is_dir() and any(destino.iterdir()) and not args.force:
        log(f"{destino} ya tiene contenido. No se toca (--force para rehacerlo).")
        return
    log(f"Trayendo '{ds['name']}' -> {destino}")
    blob = resolver_blob(ds)
    if not args.no_check:
        verificar(ds, blob)
    destino.mkdir(parents=True, exist_ok=True)
    with tarfile.open(blob) as tar:
        # `data` filtra rutas absolutas y ../ del tar. Sin esto, un tar hostil
        # escribe fuera del destino; y aunque este sea nuestro, la maquina donde
        # se desempaqueta a veces no lo es.
        try:
            tar.extractall(destino, filter="data")  # type: ignore[call-arg]
        except TypeError:  # Python < 3.12 no tiene `filter`
            tar.extractall(destino)
    n = sum(1 for _ in destino.rglob("*") if _.is_file())
    log(f"Listo: {n} ficheros en {destino}")


def cmd_check(args: argparse.Namespace) -> None:
    fallos = 0
    for ds in ([load_dataset(args.name)] if args.name else all_datasets()):
        destino = destino_de(ds)
        blob = blob_de(ds)
        estado = []
        estado.append("desempaquetado" if destino.is_dir() and any(
            destino.rglob("*")) else "SIN desempaquetar")
        if blob.is_file():
            ok = (not ds.get("sha256")) or sha256_de(blob) == ds["sha256"]
            estado.append("blob ok" if ok else "BLOB NO CUADRA")
            fallos += 0 if ok else 1
        else:
            estado.append("sin blob en el repo")
        log(f"{ds['name']:<22} {', '.join(estado)}")
    raise SystemExit(1 if fallos else 0)


def cmd_list(args: argparse.Namespace) -> None:
    todos = all_datasets()
    if not todos:
        log("No hay ningun dataset declarado en datasets/.")
        return
    for ds in todos:
        tam = ds.get("bytes") or 0
        fuentes = ",".join(f.get("tipo", "?") for f in ds.get("fuentes", []))
        log(f"{ds['name']:<22} {tam / 1e6:>6.1f} MB  [{fuentes}]  {ds.get('descripcion', '')}")
        log(f"{'':<22} -> {ds.get('destino')}")


def main() -> None:
    force_utf8_output()
    load_env()
    parser = argparse.ArgumentParser(
        prog="dataset.py",
        description="Datasets verificados que llegan a cualquier maquina.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("list", help="datasets declarados en datasets/")
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("pack", help="fabrica el tar.gz desde la copia local y da su sha256")
    p.add_argument("name")
    p.add_argument("--origen", help="directorio a empaquetar, si no el de la fuente 'local'")
    p.add_argument("--out", help="ruta del tar.gz (por defecto datasets/blobs/<name>.tar.gz)")
    p.set_defaults(func=cmd_pack)

    p = sub.add_parser("fetch", help="trae el dataset y lo deja listo para usar")
    p.add_argument("name")
    p.add_argument("--dest", help="directorio destino, si no el del descriptor")
    p.add_argument("--force", action="store_true", help="rehacer aunque ya exista")
    p.add_argument("--no-check", action="store_true", help="saltarse el sha256 (no lo hagas)")
    p.set_defaults(func=cmd_fetch)

    p = sub.add_parser("check", help="que hay en disco y si cuadra con lo declarado")
    p.add_argument("name", nargs="?")
    p.set_defaults(func=cmd_check)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
