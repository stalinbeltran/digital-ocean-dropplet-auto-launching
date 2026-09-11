#!/usr/bin/env python3
"""Que la espera al arranque NUNCA falle en silencio.

    python3 tests/test_espera_arranque.py

Sin framework y sin dependencias, como el resto de este repo.

QUE SE FIJA AQUI, y por que esto y no "que funcione": el 2026-09-10 por la
noche dos `launch dev` seguidos desde el mini murieron con "se agoto la espera"
y NADA MAS -ni si la maquina iba lenta, ni si estaba atascada, ni si la sonda
llegaba siquiera a ella-. Encontrar la causa costo una manana entera y dos
droplets lanzados a mano al dia siguiente, y aun asi la unica conclusion
posible fue "no se sabe": el bucle no habia guardado un solo dato.

La raiz es que una sonda que falla por SSH deja `stdout` vacio, exactamente
igual que un "todavia no", asi que los dos casos se confundian. De ahi lo que
se fija abajo, que son cuatro cosas y ninguna es cosmetica:

  - READY y FAILED siguen mandando (no se ha roto lo que ya funcionaba);
  - una sonda que NO LLEGA se distingue de una que dice "todavia no";
  - el mensaje del plazo agotado lleva el diagnostico DENTRO, porque cuando se
    lanza desde el movil el coordinador solo publica stderr: un parte impreso
    por el camino no llega a ningun sitio;
  - y ese mensaje avisa de que el droplet SIGUE VIVO Y FACTURANDO, que es lo
    que ayer nadie dijo y dejo dos maquinas de 24 $/mes encendidas sin motivo.

Ademas, `diagnostico_de_arranque` se llama cuando algo ya ha ido mal, asi que
no puede levantar nunca: taparia el fallo que estaba contandose.
"""

import importlib.util
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def cargar():
    spec = importlib.util.spec_from_file_location(
        "do_droplet", ROOT / "scripts" / "do_droplet.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class Salida:
    """Lo que devolveria subprocess.run, con lo justo que mira el codigo."""

    def __init__(self, stdout="", stderr="", returncode=0):
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


def main() -> int:
    mod = cargar()
    fallos = 0

    def caso(nombre: str, ok: bool, detalle: str = "") -> None:
        nonlocal fallos
        fallos += not ok
        print(f"  {'ok   ' if ok else 'FALLO'} {nombre}{'  -> ' + detalle if detalle else ''}")

    dicho: list[str] = []
    mod.log = lambda m: dicho.append(str(m))
    mod.time.sleep = lambda s: None

    # --- READY y FAILED siguen mandando -----------------------------------
    mod.subprocess.run = lambda *a, **k: Salida(stdout="READY\n")
    try:
        mod.wait_for_dev_tools("1.2.3.4", 22, timeout=5)
        caso("READY termina la espera", True)
    except SystemExit as exc:
        caso("READY termina la espera", False, f"murio con {exc}")

    mod.subprocess.run = lambda *a, **k: Salida(stdout="FAILED\n")
    try:
        mod.wait_for_dev_tools("1.2.3.4", 22, timeout=5)
        caso("FAILED mata el lanzamiento", False, "siguio como si nada")
    except SystemExit:
        caso("FAILED mata el lanzamiento", True)

    # --- el plazo se agota: que el error CUENTE algo -----------------------
    # La sonda contesta WAIT para siempre: la maquina esta viva y contesta, pero
    # no acaba. Es el caso lento, no el roto.
    mod.subprocess.run = lambda *a, **k: Salida(stdout="WAIT\n")
    mod.diagnostico_de_arranque = lambda ip, port, timeout=45: "  - cloud-init: status: running"
    try:
        mod.wait_for_dev_tools("1.2.3.4", 22, timeout=0.01)
        caso("agotar el plazo mata el lanzamiento", False, "siguio como si nada")
    except SystemExit:
        caso("agotar el plazo mata el lanzamiento", True)
    # `die` escribe en stderr y levanta SystemExit, asi que el texto se coge de
    # donde se escribe. Se repite la espera capturandolo.
    import io
    from contextlib import redirect_stderr
    buf = io.StringIO()
    with redirect_stderr(buf):
        try:
            mod.wait_for_dev_tools("1.2.3.4", 22, timeout=0.01)
        except SystemExit:
            pass
    msg = buf.getvalue()
    caso("el error avisa de que la maquina sigue facturando",
         "FACTURANDO" in msg, "sin esto el droplet se queda encendido y nadie lo sabe")
    caso("el error lleva el diagnostico dentro",
         "cloud-init: status: running" in msg,
         "es lo unico que llega al chat: el coordinador solo publica stderr")
    caso("el error dice como rematar y como destruir",
         "provision" in msg and "destroy" in msg)
    # El 2026-09-10, tras el fallo, el usuario probo a escribirle al bot de esa
    # maquina y no contesto nunca, y lo leyo como "ademas se rompio algo". No se
    # habia roto nada: `launch` muere AQUI, que es antes de `provision`, o sea
    # antes de los secretos, los repos y los servicios. La maquina estaba a
    # medio hacer y nada lo decia.
    caso("el error avisa de que la maquina esta a medio hacer",
         "ni repos" in msg and "Telegram" in msg,
         "un bot que no contesta se lee como averia, y solo es que no esta puesto")

    # --- una sonda que NO LLEGA no es un 'todavia no' ----------------------
    dicho.clear()
    mod.subprocess.run = lambda *a, **k: Salida(
        stdout="", stderr="ssh: connect to host 1.2.3.4 port 22: Connection refused\n",
        returncode=255)
    buf = io.StringIO()
    with redirect_stderr(buf):
        try:
            mod.wait_for_dev_tools("1.2.3.4", 22, timeout=0.01)
        except SystemExit:
            pass
    caso("si el SSH no llega, se dice por que",
         any("Connection refused" in d for d in dicho),
         "stdout vacio es indistinguible de 'todavia no'; el stderr no")
    caso("y el motivo acaba tambien en el error final",
         "Connection refused" in buf.getvalue())

    # No se repite el mismo motivo una vez por sonda: quince minutos de la misma
    # linea no informan, tapan.
    repes = [d for d in dicho if "Connection refused" in d]
    caso("el mismo motivo no se repite en bucle", len(repes) == 1,
         f"se dijo {len(repes)} vez/veces")

    # --- el diagnostico no puede levantar NUNCA ---------------------------
    for nombre, efecto in (
        ("si ssh revienta", lambda *a, **k: (_ for _ in ()).throw(OSError("boom"))),
        ("si ssh se cuelga",
         lambda *a, **k: (_ for _ in ()).throw(subprocess.TimeoutExpired("ssh", 1))),
    ):
        # Modulo limpio: arriba se sustituyo `diagnostico_de_arranque` por un
        # doble, y lo que se quiere probar aqui es el de verdad.
        limpio = cargar()
        limpio.subprocess.run = efecto
        try:
            texto = limpio.diagnostico_de_arranque("1.2.3.4", 22, timeout=1)
            caso(f"el diagnostico vuelve {nombre}", bool(texto), texto.strip()[:60])
        except Exception as exc:  # noqa: BLE001
            caso(f"el diagnostico vuelve {nombre}", False, f"levanto {type(exc).__name__}")

    # Y una maquina que contesta vacio tampoco lo rompe.
    limpio = cargar()
    limpio.subprocess.run = lambda *a, **k: Salida(stdout=b"", stderr=b"")
    try:
        texto = limpio.diagnostico_de_arranque("1.2.3.4", 22, timeout=1)
        caso("el diagnostico vuelve si la maquina calla", "no contest" in texto, texto.strip()[:60])
    except Exception as exc:  # noqa: BLE001
        caso("el diagnostico vuelve si la maquina calla", False, f"levanto {type(exc).__name__}")

    # --- un arranque MUY lento que acaba bien tampoco puede pasar mudo ----
    # Subir el plazo dejo de perder lanzamientos, pero de paso convertia el
    # arranque patologico en un exito sin rastro. Va por stdout a proposito: en
    # un `launch` que sale con 0, eso es lo que el coordinador publica al chat.
    dicho.clear()
    limpio = cargar()
    limpio.log = lambda m: dicho.append(str(m))
    limpio.time.sleep = lambda s: None
    limpio.diagnostico_de_arranque = lambda ip, port, timeout=45: "  - cloud-init: status: done"
    # Reloj de mentira: el `inicio` y la comprobacion del plazo ven 0, y la
    # medicion de cuanto tardo ve 9999. Asi no hay que esperar de verdad.
    # ⚠ `limpio.time` es el modulo `time` GLOBAL -importlib da un modulo nuevo
    # para do_droplet, no para lo que este importa-, asi que esto parchea el
    # reloj de TODO el proceso. Sin restaurarlo despues, los casos siguientes
    # ven un reloj parado y sus bucles no vencen nunca: colgaron el test.
    reloj_real = limpio.time.time
    lento = iter([0.0, 0.0])
    limpio.time.time = lambda: next(lento, 9999.0)
    limpio.subprocess.run = lambda *a, **k: Salida(stdout="READY\n")
    limpio.wait_for_dev_tools("1.2.3.4", 22, timeout=99999)
    caso("un arranque lentisimo se denuncia aunque acabe bien",
         any("tard" in d for d in dicho),
         "si no, el plazo mas largo esconde justo lo que hay que ver")

    limpio.time.time = reloj_real  # ver el aviso de arriba
    caso("el umbral de sospecha es el doble de lo peor medido",
         mod.LENTO_SOSPECHOSO == 600, "272 s y 348 s el 2026-09-11")

    # --- una clave RECHAZADA no se arregla esperando ----------------------
    # El 2026-09-11 esto costo 30 min por lanzamiento, y dos dias de no dar con
    # ello: el bot del mini arrancaba antes de que existiera DO_SSH_KEY_FILE en
    # `dev-secrets.env`, caia al defecto `~/.ssh/do_droplet` -una clave que nadie
    # registro en la cuenta- y sondeaba con "Permission denied" hasta agotar el
    # plazo. Un droplet solo acepta las claves registradas CUANDO SE CREO, asi
    # que ese rechazo no iba a cambiar nunca.
    dicho.clear()
    limpio = cargar()
    limpio.log = lambda m: dicho.append(str(m))
    limpio.time.sleep = lambda s: None
    limpio.diagnostico_de_arranque = lambda ip, port, timeout=45: "  - cloud-init: status: done"
    limpio.subprocess.run = lambda *a, **k: Salida(
        stdout="", stderr="root@1.2.3.4: Permission denied (publickey)." + chr(10), returncode=255)
    buf = io.StringIO()
    t0 = __import__("time").time()
    with redirect_stderr(buf):
        try:
            limpio.wait_for_dev_tools("1.2.3.4", 22, timeout=99999)
            murio = False
        except SystemExit:
            murio = True
    msg = buf.getvalue()
    caso("una clave rechazada mata la espera en vez de agotar el plazo", murio,
         "con timeout=99999: si esperase, este test no volveria")
    caso("y el error dice CON QUE CLAVE se estaba intentando",
         "DO_SSH_KEY_FILE" in msg and "keys" in msg,
         "el dato que faltaba: la clave por defecto puede no ser la tuya")
    caso("y recuerda que la maquina sigue facturando",
         "FACTURANDO" in msg)
    caso("no se rinde a la primera sonda", limpio.RECHAZOS_FATALES >= 3,
         f"son {limpio.RECHAZOS_FATALES}: la primera puede caer con sshd colocandose")

    # Y el simetrico: un fallo de SSH que NO es de clave sigue esperando, porque
    # "connection refused" durante el arranque es normal y se arregla solo.
    limpio2 = cargar()
    limpio2.log = lambda m: None
    limpio2.time.sleep = lambda s: None
    limpio2.diagnostico_de_arranque = lambda ip, port, timeout=45: ""
    limpio2.subprocess.run = lambda *a, **k: Salida(
        stdout="", stderr="ssh: connect to host 1.2.3.4 port 22: Connection refused" + chr(10),
        returncode=255)
    buf2 = io.StringIO()
    with redirect_stderr(buf2):
        try:
            limpio2.wait_for_dev_tools("1.2.3.4", 22, timeout=0.01)
        except SystemExit:
            pass
    caso("un 'connection refused' NO mata: eso si se arregla solo",
         "RECHAZA la clave" not in buf2.getvalue(),
         "durante el arranque sshd se reinicia, y eso es normal")

    # --- el techo de la espera es configurable ----------------------------
    caso("el plazo por defecto sale de la configuracion",
         mod.DEFAULTS.get("DO_DEV_TOOLS_TIMEOUT") == "1800",
         "900 s se quedo corto dos veces seguidas el 2026-09-10")

    total = 21
    print(f"\n{total - fallos}/{total} pasan")
    return 1 if fallos else 0


if __name__ == "__main__":
    sys.exit(main())
