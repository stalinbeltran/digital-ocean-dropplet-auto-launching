#!/usr/bin/env python3
"""Un volumen se identifica por NOMBRE + REGIÓN, no por el nombre solo.

    python3 tests/test_volumen_region.py

Sin framework y sin dependencias, como el resto del repo.

POR QUÉ EXISTE (2026-10-07): nyc1 dejó de ofrecer los planes del mini y del dev, la flota
se mudó a sfo2, y un volumen no se mueve de región: el almacén se copió a un `datos` NUEVO
en sfo2, y el de nyc1 se queda (regla del dueño: `volume destroy datos` no se corre nunca).
Desde ese día hay DOS volúmenes con el mismo nombre, y `find_volume` devolvía «el primero
que diga la API». Con eso, `resize` y `destroy` podían actuar sobre el equivocado sin
avisar, y `volume create datos --region sfo2` contestaba «ya existe» y no creaba nada.

QUÉ SE FIJA:
 1. Con región, se coge el de esa región. Sin región y con homónimos, se MUERE
    nombrándolos (R16: el nombre no identifica; R2: falla antes de tocar nada).
 2. `destroy` ambiguo muere ANTES de pedir confirmación y antes de llamar a la API.
 3. `create` mira la existencia en SU región.
 4. `attach` coge el homónimo de la región del droplet.
 5. Todo tipo con `volume` declara `region` (R4: se declara, no se hereda del default).
 6. `almacen instalar` no espeja desde GitHub si no se le pide: un volumen nuevo y vacío
    recibiría la copia congelada, con otra historia, y el volcado restaurado encima la
    mezclaría. Y el log de pushes se re-adueña (`chown -R`), porque tras restaurar puede
    venir con el uid de otra máquina y el hook escribe con `|| true`.
"""

import contextlib
import importlib.util
import io
import json
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]


def cargar():
    spec = importlib.util.spec_from_file_location(
        "do_droplet", ROOT / "scripts" / "do_droplet.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def vol(id_, region, droplet=None):
    return {"id": id_, "name": "datos", "size_gigabytes": 1, "region": {"slug": region},
            "droplet_ids": [droplet] if droplet else []}


DOS = [vol("v-nyc", "nyc1"), vol("v-sfo", "sfo2", 555)]


def args_volume(**kw):
    base = dict(action="", name="datos", droplet=None, port=None, region=None, size_gb=None,
                description=None, no_mount=False, yes=True)
    base.update(kw)
    return SimpleNamespace(**base)


def main() -> int:
    mod = cargar()
    fallos = 0

    def caso(nombre: str, ok: bool, detalle: str = "") -> None:
        nonlocal fallos
        fallos += not ok
        print(f"  {'ok   ' if ok else 'FALLO'} {nombre}{'  -> ' + detalle if detalle else ''}")

    # La API de mentira: filtra por región como hace /v2/volumes?region=.
    cuenta = {"vols": DOS}
    mod.volumes = lambda region="": [v for v in cuenta["vols"]
                                     if not region or v["region"]["slug"] == region]
    llamadas = []
    mod.api = lambda metodo, ruta, cuerpo=None: (
        llamadas.append((metodo, ruta, cuerpo)) or
        {"volume": {"id": "nuevo"}, "action": {"id": 1}})
    mod.wait_for_action = lambda _id: None
    mensajes = []
    mod.log = lambda m="": mensajes.append(str(m))

    def muere(fn):
        err = io.StringIO()
        try:
            with contextlib.redirect_stderr(err):
                fn()
        except SystemExit:
            return True, err.getvalue()
        return False, err.getvalue()

    # 1. find_volume
    caso("con región: coge el de esa región",
         mod.find_volume("datos", "sfo2")["id"] == "v-sfo")
    murio, msg = muere(lambda: mod.find_volume("datos"))
    caso("sin región y dos homónimos: muere", murio)
    caso("…y el aviso nombra las dos regiones", "nyc1" in msg and "sfo2" in msg, msg[:120])
    cuenta["vols"] = [vol("v-nyc", "nyc1")]
    caso("un solo volumen sin región: sigue funcionando como antes",
         mod.find_volume("datos")["id"] == "v-nyc")
    cuenta["vols"] = DOS

    # 2. destroy ambiguo: muere antes de confirmar y de llamar a la API
    preguntado = []
    mod.confirmar = lambda _p: preguntado.append(1) or True
    llamadas.clear()
    murio, _ = muere(lambda: mod.cmd_volume(args_volume(action="destroy", yes=False)))
    caso("destroy sin región con homónimos: muere", murio)
    caso("…antes de pedir confirmación y sin tocar la API",
         not preguntado and not llamadas, f"{preguntado} {llamadas}")
    murio, _ = muere(lambda: mod.cmd_volume(args_volume(action="resize", size_gb=5)))
    caso("resize sin región con homónimos: muere sin tocar la API", murio and not llamadas)

    # 3. create mira SU región
    cuenta["vols"] = [vol("v-nyc", "nyc1")]
    llamadas.clear()
    mod.cmd_volume(args_volume(action="create", region="sfo2", size_gb=1))
    post = [c for c in llamadas if c[0] == "POST" and c[1] == "/v2/volumes"]
    caso("create en sfo2 con homónimo en nyc1: crea, y en sfo2",
         len(post) == 1 and post[0][2]["region"] == "sfo2", str(post))
    llamadas.clear()
    mod.cmd_volume(args_volume(action="create", region="nyc1", size_gb=1))
    caso("create donde YA existe: no crea otro", not llamadas, str(llamadas))
    cuenta["vols"] = DOS

    # 4. attach coge el de la región del droplet
    cuenta["vols"] = [vol("v-nyc", "nyc1", 111), vol("v-sfo", "sfo2")]
    mod.resolve_target = lambda nombre, port=0: (
        {"id": 999, "name": nombre, "region": {"slug": "sfo2"}}, "1.2.3.4", 22)
    mod.run_remote_script = lambda ip, port, script: 0
    llamadas.clear()
    mod.cmd_volume(args_volume(action="attach", droplet="mini"))
    attach = [c for c in llamadas if c[0] == "POST" and "actions" in c[1]]
    caso("attach a un droplet de sfo2: conecta el datos de sfo2 (aunque el de nyc1 exista)",
         len(attach) == 1 and "v-sfo" in attach[0][1] and attach[0][2]["region"] == "sfo2",
         str(attach))
    cuenta["vols"] = DOS

    # 5. todo tipo con volumen declara región
    sin_region = []
    for f in sorted((ROOT / "types").glob("*.json")):
        d = json.loads(f.read_text())
        if d.get("volume") and not d.get("region"):
            sin_region.append(f.stem)
    caso("todo tipo con `volume` declara `region`", not sin_region, str(sin_region))
    caso("el almacén declara su región (la del mini)",
         mod.almacen_declarado()["region"] == json.loads(
             (ROOT / "types" / "mini.json").read_text())["region"])

    # 6. instalar no espeja sin pedirlo, y re-adueña el log
    alm = mod.almacen_declarado()
    sin = mod.script_instalar_almacen(alm, "deploy", Path("/x/flota.pub"))
    con = mod.script_instalar_almacen(alm, "deploy", Path("/x/flota.pub"), espejar=True)
    # sin espejar, el `exit 1` tiene que llegar ANTES que cualquier clone
    pos_exit, pos_clone = sin.find("exit 1\nfi"), sin.find("clone -q --mirror")
    caso("instalar sin --espejar-desde-github: un repo que falta es un fallo",
         pos_exit != -1 and (pos_clone == -1 or pos_exit < pos_clone))
    caso("instalar con --espejar-desde-github: sí espeja (la primera vez de verdad)",
         "clone -q --mirror" in con and "NO está en el volumen" not in con)
    caso("instalar re-adueña el log de pushes (chown -R)", 'chown -R "$U:$U" "$LOGD"' in sin)

    # 7. el default ya no es nyc1
    caso("DO_REGION por defecto: sfo2", mod.DEFAULTS["DO_REGION"] == "sfo2")

    print(f"\n{'TODO BIEN' if not fallos else str(fallos) + ' FALLO(S)'}")
    return 1 if fallos else 0


if __name__ == "__main__":
    sys.exit(main())
