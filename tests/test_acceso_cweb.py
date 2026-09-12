#!/usr/bin/env python3
"""El acceso a la web movil es un DATO: lo que el lanzador tiene que declarar para que
Tailscale y Cloudflare convivan y la vuelta atras sea una variable.

    python3 tests/test_acceso_cweb.py

Sin framework ni dependencias, como el resto.

Por que existe (decision P14 de la app, 2026-09-12): Tailscale quedo marcado como
valido y se prueba Cloudflare Tunnel + Access. Si el lanzador no declara las tres
variables en el llavero y en el entorno, o si el servicio sigue llamando a los
scripts de Tailscale a pelo, el modo no se puede elegir desde el llavero y la prueba
no es reversible.
"""

import importlib.util
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def cargar():
    spec = importlib.util.spec_from_file_location(
        "do_droplet", ROOT / "scripts" / "do_droplet.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.log = lambda *_a, **_k: None
    return mod


def test_llavero_declara_el_acceso_y_es_opcional():
    llavero = json.loads((ROOT / "llavero.json").read_text(encoding="utf-8"))
    nombres = {v["nombre"]: v for v in llavero["variables"]}
    for n in ("CWEB_ACCESO", "CWEB_CF_TUNNEL_TOKEN", "CWEB_CF_HOSTNAME"):
        assert n in nombres, f"{n} no esta en llavero.json: no viajaria al dev ni al mini"
        assert nombres[n]["obligatoria"] is False, (
            f"{n} obligatoria: dejaria sin lanzar a quien siga en Tailscale")
        assert nombres[n]["porque"], f"{n} sin porque"


def test_entorno_mapea_los_nombres_que_lee_la_app():
    mod = cargar()
    ent = mod.load_entorno("claude-code-webapp-mobile")
    mapa = {v["nombre"]: v["desde"] for v in ent["variables"]}
    assert mapa.get("CF_TUNNEL_TOKEN") == "CWEB_CF_TUNNEL_TOKEN", mapa
    assert mapa.get("CF_HOSTNAME") == "CWEB_CF_HOSTNAME", mapa
    assert mapa.get("CWEB_ACCESO") == "CWEB_ACCESO", mapa
    assert mapa.get("CWEB_TS_ESQUEMA") == "CWEB_TS_ESQUEMA", (
        "sin esto CWEB_TS_ESQUEMA=https del llavero no llegaba a la app por ningun camino")


def test_el_servicio_pasa_por_el_despachador():
    """install y pre_destroy van por acceso.mjs: es lo que hace que el modo se elija por dato."""
    svc = json.loads((ROOT / "services" / "claude-web.json").read_text(encoding="utf-8"))
    assert svc["install"] == "node scripts/acceso.mjs unir", svc["install"]
    assert svc["pre_destroy"] == "node scripts/acceso.mjs desunir --si", svc["pre_destroy"]
    # Y sigue sin cd: el coordinador y provision ponen el cwd en la raiz del repo.
    assert "cd " not in svc["install"] and "cd " not in svc["pre_destroy"]


def test_pre_destroy_sigue_saliendo_con_0():
    """La regla de pre_destroy no cambia por cambiar de script: nunca impide destruir."""
    mod = cargar()
    guion = mod.pre_destroy_script()
    assert "acceso.mjs desunir --si" in guion, guion[-400:]


if __name__ == "__main__":
    pruebas = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for prueba in pruebas:
        prueba()
        print(f"ok  {prueba.__name__}")
    print(f"\n{len(pruebas)} tests, todos bien.")
