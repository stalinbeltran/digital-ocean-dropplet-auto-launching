#!/usr/bin/env python3
"""La clave con la que se ENTRA: que no dependa del entorno y que se compruebe gratis.

    python3 tests/test_clave_de_entrada.py

Sin framework ni dependencias, como el resto.

De donde sale esto. El 2026-09-11 un `launch mini` lanzado desde el bot de un
dev murio con `root@161.35.50.148: Permission denied (publickey)` intentando
`/home/deploy/.ssh/do_droplet`, que en esa maquina no existe, teniendo la clave
de la flota buena al lado. El droplet estaba PERFECTO -nace con todas las claves
de la cuenta, y la de la flota es una de ellas-; lo que no valia era la clave
elegida en el lado que lanza.

`DO_SSH_KEY_FILE` no lo declara nada del repo: lo escribe `_mandar_clave_flota()`
en `dev-secrets.env`, asi que solo llega por el ENTORNO. Y el entorno de un
servicio es una FOTO de cuando arranco, o sea que el bot de una maquina que nacio
antes de esa linea nunca la ve. Por eso se fijan dos cosas aqui:

  - `fichero_clave_ssh()` cae a la clave de la flota cuando la configurada NO
    EXISTE. No es adivinar: la de la flota esta registrada en la cuenta por
    definicion, y la que no existe no puede autenticar nada.
  - `comprobar_clave_de_entrada()` mira ANTES DE CREAR que esa clave este entre
    las que el droplet va a llevar. El fallo tiene que salir gratis; antes salia
    con la maquina creada, facturando y a medio hacer.
"""

import importlib.util
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# Material de una publica de verdad, recortado: lo que se compara es el segundo
# campo, que es el unico que identifica la clave. El comentario (tercer campo) no
# cuenta, y eso tambien se prueba.
MAT_FLOTA = "AAAAC3NzaC1lZDI1NTE5AAAAIFLOTAflotaFLOTAflotaFLOTAflotaFLOTAxx"
MAT_OTRA = "AAAAC3NzaC1lZDI1NTE5AAAAIOTRAotraOTRAotraOTRAotraOTRAotraOTRA0"


class Muerte(Exception):
    pass


def cargar():
    spec = importlib.util.spec_from_file_location(
        "do_droplet", ROOT / "scripts" / "do_droplet.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.salida = []
    mod.log = lambda *a, **k: mod.salida.append(" ".join(str(x) for x in a))

    def morir(msg, code=1):
        raise Muerte(msg)

    mod.die = morir
    return mod


def par(directorio: Path, nombre: str, material: str, comentario: str = "prueba"):
    """Deja un par de claves de mentira en disco y devuelve la ruta privada."""
    priv = directorio / nombre
    priv.write_text("-----PRIVADA DE MENTIRA-----\n", encoding="utf-8")
    (directorio / (nombre + ".pub")).write_text(
        f"ssh-ed25519 {material} {comentario}\n", encoding="utf-8")
    return priv


def entorno(mod, configurada, flota):
    import os

    os.environ["DO_SSH_KEY_FILE"] = str(configurada)
    os.environ["DO_FLEET_KEY_FILE"] = str(flota)


# ------------------------------------------------------- fichero_clave_ssh()


def test_usa_la_configurada_si_existe(mod):
    """Si la clave configurada existe, no se toca nada: manda ella."""
    with tempfile.TemporaryDirectory() as d:
        d = Path(d)
        conf = par(d, "do_droplet", MAT_OTRA)
        flo = par(d, "do_flota", MAT_FLOTA)
        entorno(mod, conf, flo)
        elegida = mod.fichero_clave_ssh()
    if elegida != conf:
        return [f"con la configurada presente eligio {elegida}"]
    return []


def test_cae_a_la_flota_si_la_configurada_no_existe(mod):
    """El caso del 2026-09-11: el bot no vio DO_SSH_KEY_FILE y el defecto no existe."""
    fallos = []
    with tempfile.TemporaryDirectory() as d:
        d = Path(d)
        flo = par(d, "do_flota", MAT_FLOTA)
        entorno(mod, d / "no-existe-esta", flo)
        elegida = mod.fichero_clave_ssh()
        if elegida != flo:
            fallos.append(f"no cayo a la clave de la flota: eligio {elegida}")
        # Y lo dice, porque si no es magia silenciosa: quien lea la salida tiene
        # que saber que el entorno de esa maquina esta incompleto.
        if not any("clave de la flota" in linea for linea in mod.salida):
            fallos.append("cayo a la flota sin avisar de que lo hacia")
    return fallos


def test_el_aviso_sale_una_sola_vez(mod):
    """La espera sondea cada 10 s; el aviso en cada sonda tapa el resto del log."""
    with tempfile.TemporaryDirectory() as d:
        d = Path(d)
        flo = par(d, "do_flota", MAT_FLOTA)
        entorno(mod, d / "no-existe-esta", flo)
        for _ in range(5):
            mod.fichero_clave_ssh()
    cuantos = sum(1 for linea in mod.salida if "clave de la flota" in linea)
    return [] if cuantos == 1 else [f"el aviso salio {cuantos} veces, esperaba 1"]


def test_sin_ninguna_clave_devuelve_la_configurada(mod):
    """Sin clave de flota no hay nada mejor que ofrecer, y no puede reventar.

    El mensaje de error tiene que poder nombrar la clave configurada, que es la
    pista de que falta la variable.
    """
    with tempfile.TemporaryDirectory() as d:
        d = Path(d)
        conf = d / "do_droplet"
        entorno(mod, conf, d / "tampoco-existe")
        elegida = mod.fichero_clave_ssh()
    return [] if elegida == conf else [f"esperaba la configurada y dio {elegida}"]


def test_ssh_command_usa_la_elegida(mod):
    """El embudo por el que pasa TODO el ssh tiene que usar la caida, no cfg()."""
    with tempfile.TemporaryDirectory() as d:
        d = Path(d)
        flo = par(d, "do_flota", MAT_FLOTA)
        entorno(mod, d / "no-existe-esta", flo)
        cmd = mod.ssh_command("10.0.0.1", 22)
    if str(flo) not in cmd:
        return [f"ssh_command no lleva la clave de la flota: {cmd}"]
    return []


# ------------------------------------------- comprobar_clave_de_entrada()


def cuenta(*materiales):
    return [
        {"id": i, "name": f"k{i}", "fingerprint": f"f{i}",
         "public_key": f"ssh-ed25519 {m} alguien@algun-sitio"}
        for i, m in enumerate(materiales, start=1)
    ]


def test_pasa_si_la_clave_esta_en_la_cuenta(mod):
    with tempfile.TemporaryDirectory() as d:
        d = Path(d)
        flo = par(d, "do_flota", MAT_FLOTA)
        entorno(mod, flo, flo)
        try:
            mod.comprobar_clave_de_entrada(cuenta(MAT_OTRA, MAT_FLOTA))
        except Muerte as e:
            return [f"murio con la clave registrada: {e}"]
    return []


def test_compara_el_material_no_el_comentario(mod):
    """Misma clave, otro comentario, es LA MISMA. Comparar la linea entera fallaria."""
    with tempfile.TemporaryDirectory() as d:
        d = Path(d)
        flo = par(d, "do_flota", MAT_FLOTA, comentario="laptop-vieja")
        entorno(mod, flo, flo)
        try:
            mod.comprobar_clave_de_entrada(cuenta(MAT_FLOTA))
        except Muerte as e:
            return [f"no reconocio la misma clave con otro comentario: {e}"]
    return []


def test_muere_si_la_clave_no_esta_en_la_cuenta(mod):
    """El fallo del 2026-09-11, pero GRATIS: sin droplet creado."""
    fallos = []
    with tempfile.TemporaryDirectory() as d:
        d = Path(d)
        conf = par(d, "mi-clave", MAT_OTRA)
        entorno(mod, conf, d / "sin-flota")
        try:
            mod.comprobar_clave_de_entrada(cuenta(MAT_FLOTA))
            fallos.append("dejo pasar una clave que no esta en la cuenta")
        except Muerte as e:
            texto = str(e)
            # El mensaje tiene que decir las dos cosas que hacen falta para
            # actuar: que no se ha creado nada, y como registrar la clave.
            if "No se ha creado" not in texto:
                fallos.append("no dice que no se ha creado ningun droplet")
            if "register-key" not in texto and "clave-flota" not in texto:
                fallos.append("no dice como arreglarlo")
    return fallos


def test_muere_si_no_existe_la_privada(mod):
    """Sin clave no se entra: eso se sabe antes de gastar un centimo."""
    fallos = []
    with tempfile.TemporaryDirectory() as d:
        d = Path(d)
        entorno(mod, d / "no-existe", d / "tampoco")
        try:
            mod.comprobar_clave_de_entrada(cuenta(MAT_FLOTA))
            fallos.append("dejo lanzar sin ninguna clave privada")
        except Muerte as e:
            if "No se ha creado" not in str(e):
                fallos.append("no dice que no se ha creado ningun droplet")
    return fallos


def test_sin_publica_avisa_y_sigue(mod):
    """No saber no es saber que va mal: con la privada sola se lanza igual.

    Bloquear aqui dejaria sin lanzar a quien tenga la privada y no la .pub, que
    es una situacion legitima y no un error.
    """
    fallos = []
    with tempfile.TemporaryDirectory() as d:
        d = Path(d)
        conf = d / "solo-privada"
        conf.write_text("-----PRIVADA DE MENTIRA-----\n", encoding="utf-8")
        entorno(mod, conf, d / "sin-flota")
        try:
            mod.comprobar_clave_de_entrada(cuenta(MAT_FLOTA))
        except Muerte as e:
            fallos.append(f"murio por no tener la .pub: {e}")
        if not any("AVISO" in linea for linea in mod.salida):
            fallos.append("siguio sin avisar de que no pudo comprobar nada")
    return fallos


# --------------------------------------------------------- orden en launch


def test_la_comprobacion_va_antes_de_crear(mod):
    """El freno nunca puede llegar despues del acelerador.

    Se mira sobre el codigo fuente a proposito: probar el orden ejecutando
    `cmd_launch` exigiria simular media API, y lo que puede romperse aqui es que
    alguien mueva la llamada unas lineas mas abajo sin darse cuenta.
    """
    fuente = (ROOT / "scripts" / "do_droplet.py").read_text(encoding="utf-8")
    corte = fuente.index("def cmd_launch(")
    cuerpo = fuente[corte:]
    try:
        freno = cuerpo.index("comprobar_clave_de_entrada(keys)")
    except ValueError:
        return ["cmd_launch ya no comprueba la clave de entrada"]
    acelerador = cuerpo.index('api("POST", "/v2/droplets"')
    if freno > acelerador:
        return ["la comprobacion de la clave corre DESPUES de crear el droplet"]
    return []


def main():
    pruebas = [
        ("la configurada manda si existe", test_usa_la_configurada_si_existe),
        ("cae a la flota si la configurada no existe",
         test_cae_a_la_flota_si_la_configurada_no_existe),
        ("el aviso de la caida sale una vez", test_el_aviso_sale_una_sola_vez),
        ("sin ninguna clave no revienta", test_sin_ninguna_clave_devuelve_la_configurada),
        ("ssh_command usa la clave elegida", test_ssh_command_usa_la_elegida),
        ("pasa si la clave esta en la cuenta", test_pasa_si_la_clave_esta_en_la_cuenta),
        ("compara el material, no el comentario", test_compara_el_material_no_el_comentario),
        ("muere si la clave no esta en la cuenta",
         test_muere_si_la_clave_no_esta_en_la_cuenta),
        ("muere si no existe la privada", test_muere_si_no_existe_la_privada),
        ("sin la .pub avisa y sigue", test_sin_publica_avisa_y_sigue),
        ("la comprobacion va antes de crear", test_la_comprobacion_va_antes_de_crear),
    ]
    total = 0
    for nombre, prueba in pruebas:
        mod = cargar()  # modulo limpio por prueba: el aviso lleva estado global
        fallos = prueba(mod)
        total += len(fallos)
        print(f"  {'ok   ' if not fallos else 'FALLO'} {nombre}")
        for fallo in fallos:
            print(f"          {fallo}")
    print(f"\n{len(pruebas)} pruebas, {total} fallo(s)")
    return 1 if total else 0


if __name__ == "__main__":
    sys.exit(main())
