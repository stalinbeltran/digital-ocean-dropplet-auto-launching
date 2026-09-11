#!/usr/bin/env python3
"""`entornos recoger`: lo que un proyecto produce vuelve al llavero, y el llavero se lee del disco.

    python3 tests/test_entornos_recoger.py

Sin framework ni dependencias, como el resto.

Lo que esto fija, y por que (medido el 2026-09-11):

  - Let's Encrypt da 5 certificados por semana y por nombre exacto, y cada dev
    rehecho pedia uno nuevo porque vivia en el droplet destruido. Para que viaje
    hace falta el camino de VUELTA (proyecto -> llavero), que no existia: solo
    habia `aplicar` (llavero -> proyecto). `recoger` es ese camino, y tiene que
    ser generico: el lanzador no sabe que es un certificado.
  - Solo viaja lo declarado en entornos/<x>.json, con el mismo mapa que `aplicar`
    pero al reves (nombre del proyecto -> nombre del llavero).
  - Se escribe donde las DEMAS maquinas lo leen: en una de la flota es
    dev-secrets.env, no el .env del repo. Y pisa: el productor es la fuente.
  - `load_env()` lee dev-secrets.env del disco. Sin eso, un secreto enviado al
    mini con `llavero enviar` era invisible para el bot hasta reiniciarlo, y el
    siguiente `launch dev` desde el bot habria pedido otro certificado sin decirlo.
"""

import importlib.util
import json
import os
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


class Muerte(Exception):
    pass


class Args:
    def __init__(self, entorno=None):
        self.entorno = entorno or []


def preparar(mod, tmp, con_secretos: bool):
    """Un llavero local en un directorio temporal, y un entorno de prueba."""
    salida = []
    mod.log = lambda *a, **k: salida.append(" ".join(str(x) for x in a))

    def _die(msg):
        raise Muerte(msg)
    mod.die = _die

    secretos = tmp / "dev-secrets.env"
    env_local = tmp / ".env"
    if con_secretos:
        secretos.write_text("export OTRA='queda'\nexport CWEB_TS_CERT_B64='viejo'\n", encoding="utf-8")
    mod.ruta_secretos_locales = lambda: secretos
    mod.ruta_env_local = lambda: env_local

    base = tmp / "src"
    (base / "proyecto").mkdir(parents=True)
    ent = {
        "name": "prueba", "dir": "proyecto", "fichero": ".env",
        "recoger": "printf 'TS_CERT_B64=abc\\nTS_KEY_B64=def\\nDE_MAS=x\\n# c\\nVACIA=\\n'",
        "variables": [
            {"nombre": "TS_CERT_B64", "desde": "CWEB_TS_CERT_B64", "obligatoria": False, "porque": ""},
            {"nombre": "TS_KEY_B64", "desde": "CWEB_TS_KEY_B64", "obligatoria": False, "porque": ""},
        ],
    }
    mod.all_entornos = lambda: [ent]
    return base, ent, secretos, env_local, salida


def test_recoger_escribe_en_dev_secrets_y_pisa():
    mod = cargar()
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        base, _ent, secretos, env_local, salida = preparar(mod, tmp, con_secretos=True)
        for n in ("CWEB_TS_CERT_B64", "CWEB_TS_KEY_B64"):
            os.environ.pop(n, None)
        mod._entornos_recoger(base, Args())
        texto = secretos.read_text(encoding="utf-8")
        assert "export CWEB_TS_CERT_B64='abc'" in texto, texto
        assert "export CWEB_TS_KEY_B64='def'" in texto, texto
        assert "export OTRA='queda'" in texto, "recoger borro una variable que no era suya"
        assert "'viejo'" not in texto, "recoger NO piso el valor viejo, y el productor es la fuente"
        assert not env_local.exists(), "escribio en el .env del repo teniendo dev-secrets.env"
        assert "DE_MAS" not in texto, "viajo una variable no declarada"
        assert any("ignoro DE_MAS" in l for l in salida), salida
        assert any("llavero enviar" in l for l in salida), "no dice como llevarlo a la otra maquina"
        # Y lo deja en el entorno del proceso, para lo que venga despues en la misma orden.
        assert os.environ.get("CWEB_TS_CERT_B64") == "abc"
        # NUNCA imprime valores.
        assert not any("abc" in l or "def" in l for l in salida), salida


def test_recoger_sin_dev_secrets_va_al_env_del_repo():
    mod = cargar()
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        base, _ent, secretos, env_local, _salida = preparar(mod, tmp, con_secretos=False)
        env_local.write_text("DO_TOKEN=x\nCWEB_TS_KEY_B64=viejo\n", encoding="utf-8")
        mod._entornos_recoger(base, Args())
        assert not secretos.exists()
        texto = env_local.read_text(encoding="utf-8")
        assert "CWEB_TS_CERT_B64=abc" in texto and "CWEB_TS_KEY_B64=def" in texto, texto
        assert "DO_TOKEN=x" in texto
        assert "viejo" not in texto


def test_recoger_falla_si_el_comando_falla():
    mod = cargar()
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        base, ent, secretos, _env_local, salida = preparar(mod, tmp, con_secretos=True)
        ent["recoger"] = "echo 'no hay certificado' >&2; exit 1"
        antes = secretos.read_text(encoding="utf-8")
        try:
            mod._entornos_recoger(base, Args())
        except SystemExit as exc:
            assert exc.code == 1
        else:
            raise AssertionError("un comando que falla tiene que salir con 1")
        assert secretos.read_text(encoding="utf-8") == antes, "toco el llavero con un fallo"
        assert any("no hay certificado" in l for l in salida), salida


def test_recoger_filtra_por_entorno_y_sin_recoger_no_hace_nada():
    mod = cargar()
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        base, _ent, secretos, _env_local, salida = preparar(mod, tmp, con_secretos=True)
        mod._entornos_recoger(base, Args(entorno=["otro"]))
        assert any("Ningún entorno declara" in l for l in salida), salida
        assert "'abc'" not in secretos.read_text(encoding="utf-8")


def test_load_env_lee_dev_secrets_del_disco_y_el_entorno_manda():
    mod = cargar()
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        secretos = tmp / "dev-secrets.env"
        secretos.write_text(
            "export DEL_DISCO='hola'\n"
            "export CON_COMILLA='it'\"'\"'s'\n"
            "export YA_ESTABA='del fichero'\n"
            "export VACIA=''\n"
            "no es export\n",
            encoding="utf-8",
        )
        mod.ruta_secretos_locales = lambda: secretos
        mod.ruta_env_local = lambda: tmp / "no-existe.env"
        for n in ("DEL_DISCO", "CON_COMILLA", "VACIA"):
            os.environ.pop(n, None)
        os.environ["YA_ESTABA"] = "del entorno"
        mod.load_env()
        assert os.environ["DEL_DISCO"] == "hola"
        assert os.environ["CON_COMILLA"] == "it's", os.environ["CON_COMILLA"]
        assert os.environ["YA_ESTABA"] == "del entorno", "el fichero piso al entorno real"
        assert "VACIA" not in os.environ


def test_lo_declarado():
    """El certificado esta declarado en los DOS sitios, y el entorno sabe recogerlo."""
    llavero = json.loads((ROOT / "llavero.json").read_text(encoding="utf-8"))
    nombres = {v["nombre"]: v for v in llavero["variables"]}
    for n in ("CWEB_TS_CERT_B64", "CWEB_TS_KEY_B64"):
        assert n in nombres, f"{n} no esta en llavero.json: no viajaria"
        assert nombres[n]["obligatoria"] is False, f"{n} obligatoria dejaria sin lanzar a quien aun no lo tenga"

    mod = cargar()
    ent = mod.load_entorno("claude-code-webapp-mobile")
    mapa = {v["nombre"]: v["desde"] for v in ent["variables"]}
    assert mapa.get("TS_CERT_B64") == "CWEB_TS_CERT_B64", mapa
    assert mapa.get("TS_KEY_B64") == "CWEB_TS_KEY_B64", mapa
    assert "cert exportar" in ent["recoger"], ent["recoger"]

    ex = json.loads((ROOT / "telegram" / "executors" / "entornos.json").read_text(encoding="utf-8"))
    assert "recoger" in ex["ejemplos"], "recoger no se puede pedir desde Telegram"
    assert "entornos {{input}}" in ex["command"]

    fuente = (ROOT / "scripts" / "do_droplet.py").read_text(encoding="utf-8")
    assert '"recoger"]' in fuente or '"recoger"' in fuente.split("add_parser(\n        \"entornos\"")[1][:600], \
        "recoger no esta en las choices del subcomando entornos"


if __name__ == "__main__":
    pruebas = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for prueba in pruebas:
        prueba()
        print(f"ok  {prueba.__name__}")
    print(f"\n{len(pruebas)} tests, todos bien.")
