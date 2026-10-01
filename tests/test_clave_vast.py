#!/usr/bin/env python3
"""Un proveedor, una clave: las rutas por defecto de los dos no pueden coincidir.

    python3 tests/test_clave_vast.py

Sin framework ni dependencias, como el resto.

Por que esto merece un test propio, siendo una linea. Hasta el 2026-09-11
`VAST_SSH_KEY_FILE` y `DO_SSH_KEY_FILE` apuntaban los dos a `~/.ssh/do_droplet`,
y estaba puesto **a proposito**: el `.env.example` lo explicaba como "por defecto
la misma que la de DigitalOcean", y cuando se escribio era razonable, porque los
droplets se entraban con esa clave.

Dejo de ser razonable al llegar la CLAVE DE FLOTA. DigitalOcean se movio a
`~/.ssh/do_flota` y `do_droplet` quedo como un fichero que EXISTE y no autentica
contra DO -- y lo peor: lo CREA el `post` de los tipos, que corre
`vast_instance.py register-key`, asi que toda maquina de la flota nacia con esa
trampa puesta en la ruta por defecto de DigitalOcean. El 2026-09-11 un `launch
mini` desde el bot de un dev murio ahi, y "si la clave no existe, usa la de la
flota" no lo arreglaba, porque el fichero SI existia.

O sea que la coincidencia no se nota cuando se introduce, se nota meses despues
y desde un chat de Telegram. De ahi el test: es la clase de linea que alguien
vuelve a igualar por comodidad.

⚠ Y la EXCEPCION, desde el 2026-10-01: en la FLOTA, Vast usa la clave de flota
por variable de entorno (`VAST_SSH_KEY_FILE` en dev-secrets.env, que escribe
`_mandar_clave_flota`), porque una clave por maquina goteaba en la cuenta de Vast
(19 ese dia, tras podarlas a 3 el 2026-09-11). Los DEFECTOS siguen distintos -lo
fijan los tres primeros tests-, y lo que hacia peligrosa la coincidencia, que
`register-key` FABRICA el par si falta, queda cerrado para cualquier ruta que no
sea el defecto: se niega. Los cinco ultimos tests son esa excepcion; medido el
2026-10-01, TRES fallan con el codigo anterior (la negativa, el llavero leido de
disco y la clave de flota para Vast) y los otros dos fijan lo que ya funcionaba
(la laptop sigue generando su par; el lanzador se monta antes del `post`).
"""

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def cargar(nombre: str):
    spec = importlib.util.spec_from_file_location(
        nombre, ROOT / "scripts" / f"{nombre}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[nombre] = mod  # vast_instance importa dataset por su nombre
    spec.loader.exec_module(mod)
    mod.log = lambda *_a, **_k: None
    return mod


def test_las_dos_rutas_son_distintas():
    do = cargar("do_droplet")
    vast = cargar("vast_instance")
    a = Path(do.DEFAULTS["DO_SSH_KEY_FILE"]).expanduser()
    b = Path(vast.DEFAULTS["VAST_SSH_KEY_FILE"]).expanduser()
    if a == b:
        return [f"los dos proveedores comparten la ruta por defecto: {a}"]
    return []


def test_vast_no_apunta_a_la_ruta_de_do():
    """Ni por la otra punta: el defecto de Vast no puede ser el de DO.

    Se compara contra el LITERAL y no solo contra el otro defecto, porque si
    alguien cambiara los dos a la vez el test de arriba seguiria pasando.
    """
    vast = cargar("vast_instance")
    b = Path(vast.DEFAULTS["VAST_SSH_KEY_FILE"]).expanduser()
    if b.name == "do_droplet" or b.name == "do_flota":
        return [f"la clave de Vast se llama como una de DigitalOcean: {b}"]
    return []


def test_el_env_example_no_las_vuelve_a_igualar():
    """La plantilla es lo que la gente copia, asi que ahi tambien.

    Un `.env` con la vieja ruta pisa el defecto, y entonces el arreglo no
    existe en esa maquina.
    """
    texto = (ROOT / ".env.example").read_text(encoding="utf-8")
    fallos = []
    for linea in texto.splitlines():
        limpia = linea.strip()
        if limpia.startswith("VAST_SSH_KEY_FILE=") and "do_droplet" in limpia:
            fallos.append(f".env.example vuelve a igualarlas: {limpia}")
    return fallos


def test_no_se_alquila_sin_clave_registrada():
    """`alquilar()` comprueba la clave ANTES del PUT que cuesta dinero.

    Va sobre el codigo fuente por lo mismo que su hermano en
    `test_clave_de_entrada.py`: ejercitarlo de verdad exigiria alquilar una
    maquina. Y va dentro de `alquilar()` y no de `cmd_launch()` a proposito,
    porque hay DOS sitios que gastan -`launch` y el barrido- y un guard que hay
    que recordar en cada sitio nuevo es una nota, no un arreglo.

    `launch` no lo hacia, y no se notaba porque el `post` de los tipos dejaba la
    clave hecha y registrada en cada maquina de la flota. Al separar la ruta de
    Vast de la de DigitalOcean ese apoyo desaparece.
    """
    fuente = (ROOT / "scripts" / "vast_instance.py").read_text(encoding="utf-8")
    corte = fuente.index("def alquilar(")
    # Hasta la siguiente definicion de nivel superior: si se leyera el fichero
    # entero, una llamada en cualquier otra funcion daria el test por bueno.
    fin = fuente.index("\ndef ", corte + 1)
    cuerpo = fuente[corte:fin]
    try:
        freno = cuerpo.index("asegurar_clave_registrada(")
    except ValueError:
        return ["alquilar() ya no comprueba la clave antes de gastar"]
    gasto = cuerpo.index('api("PUT", f"/api/v0/asks/')
    if freno > gasto:
        return ["la comprobacion de la clave corre DESPUES del PUT que alquila"]
    return []


def _cuenta_vast(*materiales):
    return [
        {"id": 1000 + i, "public_key": f"ssh-ed25519 {m} x"}
        for i, m in enumerate(materiales)
    ]


def test_prune_nunca_borra_la_de_esta_maquina():
    """Es el unico comando destructivo del fichero, y ese es el borrado que no se vuelve.

    En Vast las claves no tienen nombre, asi que `--prune` borra "todas menos
    las protegidas" en vez de "las que encajen". Con esa forma, la proteccion de
    la clave propia no es una comodidad: es lo unico que impide que un barrido
    deje a la maquina que lo ejecuta sin poder entrar en lo que alquile.
    """
    import tempfile

    vast = cargar("vast_instance")
    borradas = []
    vast.api = lambda metodo, ruta, cuerpo=None: borradas.append(ruta)
    vast.confirmar = lambda _p: True
    mia = "AAAAmiaAAAA"
    with tempfile.TemporaryDirectory() as d:
        pub = Path(d) / "vast.pub"
        pub.write_text(f"ssh-ed25519 {mia} esta-maquina\n", encoding="utf-8")
        vast.clave_publica = lambda: pub
        claves = _cuenta_vast("AAAAmuerta1AAAA", mia, "AAAAmuerta2AAAA", "AAAAotraVivaAAAA")
        # La cuarta es de otra maquina viva y se declara con --keep.
        vast._podar_claves(claves, keep=[1003], yes=True)

    fallos = []
    if "/api/v0/ssh/1001/" in borradas:
        fallos.append("borro la clave de ESTA maquina")
    if "/api/v0/ssh/1003/" in borradas:
        fallos.append("borro una clave protegida con --keep")
    if sorted(borradas) != ["/api/v0/ssh/1000/", "/api/v0/ssh/1002/"]:
        fallos.append(f"borro lo que no debia: {borradas}")
    return fallos


def test_prune_pide_confirmacion():
    """Sin --yes y sin decir 'si', no se borra nada."""
    import tempfile

    vast = cargar("vast_instance")
    borradas = []
    vast.api = lambda metodo, ruta, cuerpo=None: borradas.append(ruta)
    vast.confirmar = lambda _p: False
    with tempfile.TemporaryDirectory() as d:
        pub = Path(d) / "vast.pub"
        pub.write_text("ssh-ed25519 AAAAmiaAAAA esta-maquina\n", encoding="utf-8")
        vast.clave_publica = lambda: pub
        vast._podar_claves(_cuenta_vast("AAAAmuertaAAAA"), keep=None, yes=False)
    return [] if not borradas else ["borro sin confirmacion"]


class _Entorno:
    """Cambia variables de entorno y las DEJA COMO ESTABAN, pase lo que pase."""

    def __init__(self, **cambios):
        import os
        self.os, self.cambios, self.antes = os, cambios, {}

    def __enter__(self):
        for k, v in self.cambios.items():
            self.antes[k] = self.os.environ.get(k)
            if v is None:
                self.os.environ.pop(k, None)
            else:
                self.os.environ[k] = v
        return self

    def __exit__(self, *_exc):
        for k, v in self.antes.items():
            if v is None:
                self.os.environ.pop(k, None)
            else:
                self.os.environ[k] = v


def test_una_ruta_elegida_que_no_existe_se_niega():
    """El cierre de la trampa: una ruta que NO es el defecto no se fabrica.

    En la flota, `VAST_SSH_KEY_FILE` es la clave de flota. Si faltara y
    `register-key` la fabricara, esa maquina tendria una `do_flota` falsa y
    dejaria de entrar en el mini y en el almacen sin ningun error. Con el codigo
    anterior al 2026-10-01 este test FALLA: generaba el par.
    """
    import tempfile

    vast = cargar("vast_instance")
    with tempfile.TemporaryDirectory() as d:
        ruta = Path(d) / "do_flota"
        with _Entorno(VAST_SSH_KEY_FILE=str(ruta)):
            try:
                vast.asegurar_clave_local(comentario="test")
            except SystemExit:
                pass
            else:
                return ["con una ruta elegida y sin par, siguio adelante"]
        if ruta.exists() or Path(str(ruta) + ".pub").exists():
            return [f"fabrico un par en {ruta}: eso suplanta la clave de flota"]
    return []


def test_la_ruta_por_defecto_si_se_genera():
    """Y la laptop no cambia: en el DEFECTO, el par se sigue generando solo."""
    import tempfile

    vast = cargar("vast_instance")
    with tempfile.TemporaryDirectory() as d:
        ruta = Path(d) / ".ssh" / "vast"
        vast.DEFAULTS["VAST_SSH_KEY_FILE"] = str(ruta)
        with _Entorno(VAST_SSH_KEY_FILE=None):
            pub = vast.asegurar_clave_local(comentario="test")
        if not ruta.exists() or not pub.startswith("ssh-ed25519 "):
            return ["en la ruta por defecto ya no se genera el par"]
    return []


def test_load_env_lee_el_llavero_de_disco():
    """Lo que la flota pone en dev-secrets.env llega aunque nadie lo cargue.

    Sin esto, un proceso que no viene de un shell de login (una unidad de
    systemd) no veia `VAST_SSH_KEY_FILE`, caia al defecto y registraba una clave
    NUEVA en Vast: el goteo otra vez. Y el `export ` se quita: si no, la variable
    se llamaria «export NOMBRE», sin error y sin efecto.
    """
    import tempfile

    vast = cargar("vast_instance")
    with tempfile.TemporaryDirectory() as d:
        vast.ROOT = Path(d)  # sin el .env del repo: sólo cuenta el llavero
        (Path(d) / ".config").mkdir()
        (Path(d) / ".config" / "dev-secrets.env").write_text(
            "export VAST_SSH_KEY_FILE='/home/x/.ssh/do_flota'\n"
            "export PRUEBA_COMILLAS='a'\"'\"'b'\n"
            "export PRUEBA_YA_ESTABA='del fichero'\n",
            encoding="utf-8",
        )
        with _Entorno(HOME=d, VAST_SSH_KEY_FILE=None, PRUEBA_COMILLAS=None,
                      PRUEBA_YA_ESTABA="del entorno"):
            vast.load_env()
            import os
            fallos = []
            if os.environ.get("VAST_SSH_KEY_FILE") != "/home/x/.ssh/do_flota":
                fallos.append(f"no leyo el llavero: {os.environ.get('VAST_SSH_KEY_FILE')!r}")
            if os.environ.get("PRUEBA_COMILLAS") != "a'b":
                fallos.append(f"no deshizo las comillas: {os.environ.get('PRUEBA_COMILLAS')!r}")
            if os.environ.get("PRUEBA_YA_ESTABA") != "del entorno":
                fallos.append("el fichero piso una variable del entorno: manda el entorno")
            if any(k.startswith("export ") for k in os.environ):
                fallos.append("dejo una variable llamada «export …»")
    return fallos


def test_la_flota_usa_su_clave_tambien_en_vast():
    """`_mandar_clave_flota` deja `VAST_SSH_KEY_FILE` apuntando a la de flota.

    Y DESPUES de escribir la clave: si la variable apuntara a un fichero que
    todavia no esta, el `register-key` del `post` se negaria (ver arriba).
    """
    import tempfile

    do = cargar("do_droplet")
    guiones = []
    do.run_remote_script = lambda _ip, _port, script, **_k: guiones.append(script) or 0
    with tempfile.TemporaryDirectory() as d:
        priv = Path(d) / "do_flota"
        priv.write_text("-----BEGIN OPENSSH PRIVATE KEY-----\nx\n", encoding="utf-8")
        Path(str(priv) + ".pub").write_text("ssh-ed25519 AAAAflota flota\n", encoding="utf-8")
        do._mandar_clave_flota("maquina", "203.0.113.7", 22, "deploy", priv)
    if len(guiones) != 1:
        return [f"esperaba un guion remoto, hubo {len(guiones)}"]
    guion = guiones[0]
    destino = do.FICHERO_CLAVE_FLOTA.replace("~/", "")
    linea = f'echo "export VAST_SSH_KEY_FILE=$H/{destino}" >> "$F"'
    fallos = []
    if linea not in guion:
        fallos.append("no apunta VAST_SSH_KEY_FILE a la clave de flota")
    elif guion.index(linea) < guion.index('cat > "$KEY"'):
        fallos.append("apunta la variable ANTES de escribir la clave")
    if 'grep -v "^export VAST_SSH_KEY_FILE="' not in guion:
        fallos.append("no quita la linea vieja: repetirlo la duplicaria")
    return fallos


def test_el_lanzador_se_hace_antes_del_post():
    """El `register-key` del `post` usa la clave que deja `hacer_lanzador`."""
    fuente = (ROOT / "scripts" / "do_droplet.py").read_text(encoding="utf-8")

    def cuerpo(nombre):
        corte = fuente.index(f"def {nombre}(")
        return fuente[corte:fuente.index("\ndef ", corte + 1)]

    launch, provision = cuerpo("cmd_launch"), cuerpo("cmd_provision")
    fallos = []
    if launch.index("cmd_provision(") > launch.index("ejecutar_post("):
        fallos.append("launch corre el post ANTES de aprovisionar")
    if "hacer_lanzador(" not in provision:
        fallos.append("provision ya no llama a hacer_lanzador: nadie pone la clave de flota")
    return fallos


def main():
    pruebas = [
        ("las dos rutas por defecto son distintas", test_las_dos_rutas_son_distintas),
        ("la de Vast no se llama como una de DO", test_vast_no_apunta_a_la_ruta_de_do),
        ("el .env.example no las vuelve a igualar",
         test_el_env_example_no_las_vuelve_a_igualar),
        ("no se alquila sin clave registrada", test_no_se_alquila_sin_clave_registrada),
        ("prune nunca borra la de esta maquina",
         test_prune_nunca_borra_la_de_esta_maquina),
        ("prune pide confirmacion", test_prune_pide_confirmacion),
        ("una ruta elegida que no existe se NIEGA",
         test_una_ruta_elegida_que_no_existe_se_niega),
        ("la ruta por defecto si se genera", test_la_ruta_por_defecto_si_se_genera),
        ("load_env lee el llavero de disco", test_load_env_lee_el_llavero_de_disco),
        ("la flota usa su clave tambien en Vast", test_la_flota_usa_su_clave_tambien_en_vast),
        ("el lanzador se hace antes del post", test_el_lanzador_se_hace_antes_del_post),
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
