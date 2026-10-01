#!/usr/bin/env python3
"""El tag `atendida` y `list --json`: la mitad de este repo del freno de DO.

    python3 tests/test_atendida.py

Sin framework ni dependencias, como el resto.

Por que existe. El freno del coordinador (`telegram-coordinator/scripts/
cerrable.mjs`) no miraba DigitalOcean: el 2026-10-01, con `prueba-almacen` -un
droplet lanzado DESDE el dev- vivo y facturando, dijo «🟢 CERRABLE». Ahora
pregunta aqui (`list --json`) y cuenta todo droplet salvo el suyo, los `control`
y los `atendida`. Este fichero fija lo que ese freno da por hecho de este lado:

  1. sólo los servicios que dan mando propio (los dos bots) declaran `atiende`;
  2. `launch` pone `atendida` según los servicios que INSTALA DE VERDAD:
     `prueba-almacen` era `--type dev --service ''`, o sea un dev SIN bot, y por
     tipo habria salido atendida -el falso verde otra vez-;
  3. el `body` de `launch` usa esas etiquetas, no sólo el tag del tipo;
  4. el contrato de `list --json`: sus campos, un precio que falta es None (un
     hueco leido como 0 es un droplet gratis que no lo es) y no se filtra por
     estado (un droplet apagado factura igual);
  5. `estado_nubes.py --nubes` da la lista unica de nubes, que el freno usa para
     decir NO SE ante una que no sabe mirar.
Su otra mitad: `telegram-coordinator/tests/cerrable-droplets.test.mjs`.
"""

import importlib.util
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BOTS = {"telegram-coordinator", "telegram-launcher"}


def cargar(nombre: str):
    spec = importlib.util.spec_from_file_location(nombre, ROOT / "scripts" / f"{nombre}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[nombre] = mod
    spec.loader.exec_module(mod)
    mod.log = lambda *_a, **_k: None
    return mod


def test_solo_los_bots_declaran_atiende():
    do = cargar("do_droplet")
    fallos = []
    for svc in do.all_services():
        if svc["name"] in BOTS and svc["atiende"] is not True:
            fallos.append(f"{svc['name']} es un bot y no declara `atiende`")
        if svc["name"] not in BOTS and svc["atiende"]:
            fallos.append(f"{svc['name']} declara `atiende` sin ser un bot: el freno lo ignoraria")
    return fallos


def test_etiquetas_segun_lo_que_se_instala():
    do = cargar("do_droplet")
    servicios_de = lambda tipo: do.selected_services(  # noqa: E731
        do.lista_unida([], do.load_type(tipo).get("services")))
    casos = [
        ("un dev, con su bot", "ephemeral", servicios_de("dev"), ["ephemeral", "atendida"]),
        ("el mini, con el suyo", "control", servicios_de("mini"), ["control", "atendida"]),
        # El caso del 2026-10-01: `--service ''` deja al dev sin servicios.
        ("prueba-almacen: --type dev --service ''", "ephemeral",
         do.selected_services(do.lista_unida([""], do.load_type("dev").get("services"))),
         ["ephemeral"]),
        ("una maquina sin servicios", "ephemeral", [], ["ephemeral"]),
    ]
    fallos = []
    for nombre, tag, servicios, esperado in casos:
        obtenido = do.etiquetas_de_lanzamiento(tag, servicios)
        if obtenido != esperado:
            fallos.append(f"{nombre}: {obtenido} (esperaba {esperado})")
    return fallos


def test_el_body_de_launch_usa_esas_etiquetas():
    """Sobre el fuente: ejercitarlo de verdad exigiria crear un droplet."""
    fuente = (ROOT / "scripts" / "do_droplet.py").read_text(encoding="utf-8")
    corte = fuente.index("def cmd_launch(")
    cuerpo = fuente[corte:fuente.index("\ndef ", corte + 1)]
    fallos = []
    if '"tags": etiquetas_de_lanzamiento(' not in cuerpo:
        fallos.append("el body de launch no pasa por etiquetas_de_lanzamiento()")
    if '"tags": [maquina["tag"]]' in cuerpo:
        fallos.append("el body de launch vuelve a llevar solo el tag del tipo")
    return fallos


def test_contrato_de_list_json():
    do = cargar("do_droplet")
    vivo = {
        "id": 1, "name": "prueba", "status": "active", "tags": ["ephemeral"],
        "size_slug": "s-2vcpu-4gb", "size": {"price_hourly": 0.03571},
        "created_at": "2026-10-01T16:17:00Z",
        "networks": {"v4": [{"type": "public", "ip_address": "203.0.113.7"}]},
    }
    apagado_sin_precio = {"id": 2, "name": "apagado", "status": "off", "tags": [],
                          "size_slug": "s-1vcpu-1gb", "created_at": "", "networks": {"v4": []}}
    a, b = do.datos_de_droplets([vivo, apagado_sin_precio])
    campos = {"id", "name", "status", "tags", "size_slug", "price_hourly", "created_at", "ip"}
    fallos = []
    if set(a) != campos:
        fallos.append(f"los campos cambiaron: {sorted(set(a) ^ campos)} (el freno lee estos)")
    if a["price_hourly"] != 0.03571 or a["ip"] != "203.0.113.7" or a["tags"] != ["ephemeral"]:
        fallos.append(f"datos mal copiados: {a}")
    if b["price_hourly"] is not None:
        fallos.append(f"un precio que falta salio como {b['price_hourly']!r}, no None")
    if b["status"] != "off":
        fallos.append("un droplet apagado no puede desaparecer: factura igual")
    return fallos


def test_estado_nubes_da_la_lista_unica():
    nubes = cargar("estado_nubes")
    r = subprocess.run([sys.executable, str(ROOT / "scripts" / "estado_nubes.py"), "--nubes"],
                       capture_output=True, text=True)
    esperado = [nombre for nombre, _s, _a in nubes.NUBES]
    obtenido = r.stdout.split()
    if r.returncode != 0 or obtenido != esperado:
        return [f"--nubes dio {obtenido!r} (código {r.returncode}); esperaba {esperado!r}"]
    return []


def main():
    pruebas = [
        ("solo los bots declaran `atiende`", test_solo_los_bots_declaran_atiende),
        ("las etiquetas salen de lo que se INSTALA", test_etiquetas_segun_lo_que_se_instala),
        ("el body de launch usa esas etiquetas", test_el_body_de_launch_usa_esas_etiquetas),
        ("el contrato de list --json", test_contrato_de_list_json),
        ("estado_nubes --nubes da la lista unica", test_estado_nubes_da_la_lista_unica),
    ]
    total = 0
    for nombre, prueba in pruebas:
        fallos = prueba()
        total += len(fallos)
        print(f"  {'ok   ' if not fallos else 'FALLO'} {nombre}")
        for fallo in fallos:
            print(f"          {fallo}")
    print(f"\n{len(pruebas)} pruebas, {total} fallo(s)")
    return 1 if total else 0


if __name__ == "__main__":
    sys.exit(main())
