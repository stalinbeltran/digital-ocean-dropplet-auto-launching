#!/usr/bin/env python3
"""El llavero: que se declare, que se compruebe ANTES de gastar, y que viaje entero.

    python3 tests/test_llavero.py

Sin framework y sin dependencias, como el resto: este repo corre con el python3
pelado del sistema, y un test que pida instalar algo no se ejecuta en la maquina
donde importa.

Lo que se fija aqui es el contrato que costo la averia del 2026-09-10:

  - una variable obligatoria que falte tiene que MATAR el comando, no avisar.
    El precedente esta medido: hasta el 2026-09-06 un repo sin clonar era un
    AVISO en mitad de cien lineas con exit 0, asi que el lanzamiento daba por
    bueno el trabajo con la maquina ya rota;
  - --sin-llavero es la salida de emergencia y tiene que seguir dejando pasar;
  - lo que el llavero declara tiene que ACABAR en dev-secrets.env del destino,
    porque el fallo real fue que el mini llevaba TG_* y no TGL_*: sabia parir un
    dev y no sabia parir un mini, y nadie se entero hasta que hubo que rehacerlo.
"""

import importlib.util
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def cargar():
    spec = importlib.util.spec_from_file_location(
        "do_droplet", ROOT / "scripts" / "do_droplet.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class Muerte(Exception):
    """Lo que `die()` levanta durante los tests, para poder comprobarlo."""


def sin_morir(mod):
    """Cambia die() por una excepcion y calla el log, para poder afirmar."""
    def _die(msg):
        raise Muerte(msg)
    mod.die = _die
    mod.log = lambda *_a, **_k: None


def entorno(mod, valores):
    """Deja el entorno EXACTAMENTE con esas variables del llavero, y ninguna mas.

    Importa que sea exacto: si el .env de quien corre el test se colara, un
    llavero incompleto pareceria completo y el test pasaria por el motivo
    equivocado, que es peor que fallar.
    """
    for var in mod.cargar_llavero():
        os.environ.pop(var["nombre"], None)
    os.environ.update(valores)


def llavero_completo(mod):
    return {v["nombre"]: f"valor-de-{v['nombre']}" for v in mod.cargar_llavero()}


def test_descriptor(mod):
    """El fichero existe, es valido y cada variable dice PARA QUE es."""
    fallos = []
    variables = mod.cargar_llavero()
    if len(variables) < 5:
        fallos.append(f"solo {len(variables)} variables; el llavero real tiene mas")
    nombres = [v["nombre"] for v in variables]
    if len(nombres) != len(set(nombres)):
        fallos.append("hay nombres repetidos")
    for var in variables:
        if not var["porque"]:
            # Una variable sin 'porque' es una que nadie sabra si puede quitar.
            fallos.append(f"{var['nombre']} no dice para que es")
        if not isinstance(var["obligatoria"], bool):
            fallos.append(f"{var['nombre']}: 'obligatoria' no es booleano")
    # Las que hacen que una maquina pueda parir a la otra. Si alguna deja de ser
    # obligatoria, volvemos al 2026-09-10 sin enterarnos.
    for imprescindible in ("DO_TOKEN", "GITHUB_TOKEN", "TG_BOT_TOKEN", "TGL_BOT_TOKEN"):
        if imprescindible not in nombres:
            fallos.append(f"falta {imprescindible} en el llavero")
        elif not next(v for v in variables if v["nombre"] == imprescindible)["obligatoria"]:
            fallos.append(f"{imprescindible} deberia ser obligatoria")
    return fallos


def test_falta_obligatoria_mata(mod):
    """Que falte una obligatoria MATA. Es el corazon del cambio."""
    fallos = []
    completo = llavero_completo(mod)
    obligatorias = [v["nombre"] for v in mod.cargar_llavero() if v["obligatoria"]]

    for nombre in obligatorias:
        parcial = dict(completo)
        del parcial[nombre]
        entorno(mod, parcial)
        try:
            mod.comprobar_llavero(False)
            fallos.append(f"falta {nombre} y NO murio")
        except Muerte as exc:
            # El mensaje tiene que nombrar la variable y decir donde mirar; si
            # no, obliga a leer el codigo justo cuando no puedes lanzar nada.
            if nombre not in str(exc):
                fallos.append(f"murio por {nombre} pero no lo nombra")
            if "secretos-desde-cero" not in str(exc):
                fallos.append(f"el error de {nombre} no dice donde conseguirlo")
    return fallos


def test_sin_llavero_deja_pasar(mod):
    """--sin-llavero es la salida de emergencia y tiene que seguir existiendo."""
    entorno(mod, {})
    try:
        pares = mod.comprobar_llavero(True)
    except Muerte as exc:
        return [f"--sin-llavero murio igual: {exc}"]
    return [] if pares == [] else ["--sin-llavero devolvio variables inventadas"]


def test_opcional_no_mata(mod):
    """Una opcional que falte NO puede parar un lanzamiento."""
    completo = llavero_completo(mod)
    opcionales = [v["nombre"] for v in mod.cargar_llavero() if not v["obligatoria"]]
    if not opcionales:
        return ["no hay ninguna opcional; el llavero entero seria bloqueante"]
    parcial = {k: v for k, v in completo.items() if k not in opcionales}
    entorno(mod, parcial)
    try:
        pares = mod.comprobar_llavero(False)
    except Muerte as exc:
        return [f"faltando solo opcionales, murio: {exc}"]
    nombres = {n for n, _ in pares}
    return [] if not (nombres & set(opcionales)) else ["devolvio opcionales vacias"]


def test_viaja_al_script(mod):
    """Lo que el llavero declara ACABA en dev-secrets.env del destino."""
    fallos = []
    entorno(mod, llavero_completo(mod))
    pares = mod.comprobar_llavero(False)
    script = mod.build_provision_script([], [], False, [], "", pares)

    for nombre, valor in pares:
        if f"export {nombre}=" not in script:
            fallos.append(f"{nombre} no llega al destino")
        elif mod.shq(valor) not in script:
            # Sin comillas, un valor con espacios o `$` rompe el script remoto o,
            # peor, ejecuta algo.
            fallos.append(f"{nombre} viaja sin entrecomillar")

    # Ni una sola variable repetida: el fichero del destino se lee con los ojos
    # cuando algo no autentica, y `export X=` dos veces esconde justo lo que uno
    # va a mirar.
    exports = [l.split("=", 1)[0] for l in script.splitlines() if l.startswith("export ")]
    repetidas = {e for e in exports if exports.count(e) > 1}
    if repetidas:
        fallos.append(f"exports repetidos: {', '.join(sorted(repetidas))}")
    if exports != sorted(exports):
        fallos.append("los exports no salen ordenados: un diff entre maquinas seria ruido")
    return fallos


def test_github_manda_sobre_el_llavero(mod):
    """El GITHUB_TOKEN comprobado gana al del llavero, no al reves.

    Importa porque `comprobar_github_token()` es quien le pregunta a GitHub si el
    token SIRVE. Si el valor crudo del llavero pisara al comprobado, esa
    comprobacion no serviria de nada y volveriamos a copiar tokens muertos.
    """
    entorno(mod, llavero_completo(mod))
    pares = mod.comprobar_llavero(False)
    script = mod.build_provision_script([], [], False, [], "TOKEN-COMPROBADO", pares)
    if "export GITHUB_TOKEN='TOKEN-COMPROBADO'" not in script:
        return ["el llavero pisa al token de GitHub ya comprobado"]
    if "export GH_TOKEN='TOKEN-COMPROBADO'" not in script:
        return ["GH_TOKEN (el que lee gh) no recibe el token comprobado"]
    return []


def test_tipos_piden_llavero(mod):
    """mini y dev lo piden; las maquinas de medir NO."""
    fallos = []
    for nombre in ("mini", "dev"):
        if not mod.tipo_pide_llavero(mod.load_type(nombre)):
            fallos.append(f"types/{nombre}.json ya no pide el llavero")
    for tipo in mod.all_types():
        if tipo["name"] in ("mini", "dev"):
            continue
        if mod.tipo_pide_llavero(tipo):
            # Objetivo 5: un secreto no viaja a donde no hace falta, y una
            # maquina de medir no crea nada.
            fallos.append(f"types/{tipo['name']}.json pide el llavero y no deberia")
    return fallos


def test_paridad_de_tipos(mod):
    """mini y dev solo pueden diferir en lo que esta permitido diferir."""
    mini, dev = mod.load_type("mini"), mod.load_type("dev")
    # Talla, plantilla de arranque, tag y bot. Todo lo demas tiene que ser igual:
    # esa es la propiedad entera de la flota simetrica.
    permitidas = {"size", "cloud_init", "tag", "services", "descripcion", "notas",
                  "name", "post", "image"}
    fallos = []
    for campo in set(mini) | set(dev):
        if campo in permitidas:
            continue
        if mini.get(campo) != dev.get(campo):
            fallos.append(
                f"'{campo}' difiere: mini={mini.get(campo)!r} dev={dev.get(campo)!r}")
    if sorted(mini.get("repos", [])) != sorted(dev.get("repos", [])):
        fallos.append("los repos ya no son los mismos en mini y dev")
    if not mini.get("make_launcher") or not dev.get("make_launcher"):
        fallos.append("alguna de las dos dejo de ser lanzadora")
    # Y la diferencia que TIENE que seguir existiendo: dos bots distintos, o una
    # de las dos se queda muda con un 409 de Telegram.
    if set(mini["services"]) & set(dev["services"]):
        fallos.append("mini y dev comparten servicio: eso es un 409 esperando")
    return fallos


def main():
    mod = cargar()
    sin_morir(mod)
    previo = dict(os.environ)

    pruebas = [
        ("el descriptor llavero.json es sano", test_descriptor),
        ("falta una obligatoria -> muere", test_falta_obligatoria_mata),
        ("--sin-llavero deja pasar", test_sin_llavero_deja_pasar),
        ("falta una opcional -> sigue", test_opcional_no_mata),
        ("el llavero viaja al destino", test_viaja_al_script),
        ("el GitHub comprobado manda", test_github_manda_sobre_el_llavero),
        ("solo mini y dev piden llavero", test_tipos_piden_llavero),
        ("mini y dev son gemelos", test_paridad_de_tipos),
    ]
    total = 0
    for nombre, prueba in pruebas:
        fallos = prueba(mod)
        total += len(fallos)
        print(f"  {'ok   ' if not fallos else 'FALLO'} {nombre}")
        for fallo in fallos:
            print(f"          {fallo}")

    os.environ.clear()
    os.environ.update(previo)
    print(f"\n{len(pruebas) - sum(1 for n, p in pruebas if False)} pruebas, "
          f"{total} fallo(s)")
    return 1 if total else 0


if __name__ == "__main__":
    sys.exit(main())
