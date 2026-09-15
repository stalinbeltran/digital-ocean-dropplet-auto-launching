#!/usr/bin/env python3
"""El token de la web movil NO viaja: lo crea la maquina que nace.

    python3 tests/test_token_cweb.py

Sin framework y sin dependencias, como el resto del repo.

DECISION DEL DUENO (2026-09-15): «el token se crea nuevo siempre».

Hasta ese dia, CWEB_TOKEN estaba declarado como opcional en el llavero y en el
entorno de la app. La idea era buena -- asi el enlace guardado en el movil
sobrevivia a rehacer el dev -- y el precio no: un secreto de larga vida guardado
en el llavero del mini y repartido a cada maquina que nace. Se eligio lo
contrario. Hoy lo crea el servidor de la app al arrancar, vive solo en esa
maquina (~/.config/cweb.env, 600) y muere con ella.

Este test fija la decision en los DOS sitios donde podria volver por descuido, y
FALLA con el codigo anterior (alli CWEB_TOKEN estaba en llavero.json).

⚠ Y fija la mitad que lo hace viable: si el token no viaja, la maquina tiene que
crearselo SOLA. Si algun dia eso se quita de la app y esto se queda, cada dev
nuevo nacera atado a loopback -- que es exactamente lo que paso el 2026-09-15.
"""

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APP = Path.home() / "src" / "claude-code-webapp-mobile"


def test_no_esta_en_el_llavero():
    llavero = json.loads((ROOT / "llavero.json").read_text(encoding="utf-8"))
    nombres = [v["nombre"] for v in llavero["variables"]]
    assert "CWEB_TOKEN" not in nombres, (
        "CWEB_TOKEN volvio al llavero: eso lo convierte otra vez en un secreto de "
        "larga vida repartido a cada maquina. Si es a proposito, cambia este test "
        "y di por que.")


def test_no_esta_en_el_entorno_de_la_app():
    ent = json.loads(
        (ROOT / "entornos" / "claude-code-webapp-mobile.json").read_text(encoding="utf-8"))
    nombres = [v["nombre"] for v in ent["variables"]]
    assert "CWEB_TOKEN" not in nombres, nombres
    # Lo que si puede viajar, porque no es secreto.
    assert "CWEB_PORT" in nombres and "CWEB_DATA_DIR" in nombres, nombres


def test_la_decision_esta_escrita_donde_se_lee():
    """R7: la razon vive donde se dispara, no solo en un commit."""
    for fich in (ROOT / "llavero.json", ROOT / "entornos" / "claude-code-webapp-mobile.json"):
        texto = fich.read_text(encoding="utf-8")
        assert "se crea nuevo siempre" in texto, f"{fich.name} no dice por que no esta"


def test_la_otra_mitad_sigue_en_pie():
    """Si el token no viaja, la app tiene que crearselo AL ARRANCAR.

    ⚠ Se salta si la app no esta clonada: este repo tiene que poder testearse en
    el mini, que no la lleva. Un test que exige un repo ajeno se convierte en un
    rojo permanente, y un rojo permanente se deja de leer.
    """
    servidor = APP / "server" / "index.mjs"
    if not servidor.exists():
        print("  (saltado: claude-code-webapp-mobile no esta clonado aqui)")
        return
    fuente = servidor.read_text(encoding="utf-8")
    assert "crear: true" in fuente, (
        "el servidor ya no crea el token al arrancar, y el llavero tampoco lo manda: "
        "cada dev nuevo naceria atado a loopback")


if __name__ == "__main__":
    for nombre, prueba in sorted(globals().items()):
        if nombre.startswith("test_"):
            prueba()
            print(f"  ok  {nombre}")
