#!/usr/bin/env python3
"""El modo `trabajo` de vast_instance.py: N trabajos, N maquinas, y que NINGUNA se quede viva.

    python3 tests/test_trabajo.py

Sin framework ni dependencias, como el resto. Nada de esto alquila: la API y el SSH se
sustituyen por dobles que apuntan lo que se les pide.

QUE SE FIJA, y en este orden porque es el orden de lo caro (R10):
 1. la maquina se DESTRUYE aunque falle el trabajo, y aunque falle la recogida;
 2. el hijo SALE CON 0 pase lo que pase (la unidad es Restart=on-failure: otro codigo
    seria otro alquiler);
 3. el seco no toca la API; sin --prefijo se niega SIN tocarla;
 4. un trabajo con libro vivo no se relanza;
 5. el padre reparte ofertas DISTINTAS y escribe el libro ANTES de lanzar ninguna unidad;
 6. el payload no lleva .git, .venv, datos/ ni .env;
 7. lo traido NUNCA pisa lo local: se aparta y se dice;
 8. `run` y `trae` son relativos al DESCRIPTOR, que asi nunca nombra su carpeta.
"""

import argparse
import importlib.util
import io
import json
import sys
import tarfile
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def cargar():
    spec = importlib.util.spec_from_file_location("vast_instance", ROOT / "scripts" / "vast_instance.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["vast_instance"] = mod
    spec.loader.exec_module(mod)
    mod.log = lambda *_a, **_k: None
    mod.avisar = lambda *_a, **_k: None
    mod.SONDEO_S = 0
    return mod


class Muerte(Exception):
    pass


def _die(msg):
    raise Muerte(msg)


def montar(tmp: Path, trabajos=None) -> tuple[Path, Path]:
    """Un repo de mentira con lo que NO debe viajar, y su descriptor."""
    repo = tmp / "repo"
    (repo / "exp" / "nn").mkdir(parents=True)
    (repo / "exp" / "nn" / "entrenar.py").write_text("print('hola')\n")
    for basura in (".git/config", ".venv/bin/python", "exp/datos/grande.npy", ".env"):
        (repo / basura).parent.mkdir(parents=True, exist_ok=True)
        (repo / basura).write_text("NO DEBE VIAJAR\n")
    desc = {
        "experimento": "prueba",
        "maquina": {"cpus": 4, "min_ram": 8, "max_price": 0.1},
        "envia": [{"origen": "repo", "destino": "repo",
                   "excluye": [".git", ".venv", "datos", ".env"]}],
        "install": "true",
        "trabajos": trabajos or [{"id": "a", "run": "echo a", "trae": ["repo/exp/nn/pesos/a"]},
                                 {"id": "b", "run": "echo b", "trae": ["repo/exp/nn/pesos/b"]}],
    }
    f = tmp / "desc.json"
    f.write_text(json.dumps(desc))
    return f, tmp / "libro"


def args(**kw):
    base = dict(descriptor=None, prefijo=None, libro=None, solo=None, horas_max=2.0,
                max_price=None, seco=False, estado=False, apagar=None, uno=None)
    base.update(kw)
    return argparse.Namespace(**base)


def sin_api(mod):
    def api(*_a, **_k):
        raise AssertionError("se llamo a la API")
    mod.api = api
    mod.token = lambda: (_ for _ in ()).throw(AssertionError("se pidio el token"))


def maquina_de_mentira(mod, *, falla_run=False, falla_traer=False):
    """Dobles de todo lo que toca una maquina. Devuelve lo que se apunto."""
    hecho = {"alquiladas": [], "destruidas": []}
    mod.alquilar = lambda oferta, etiqueta, image, disk: (hecho["alquiladas"].append(oferta["id"]) or 777)
    mod.destruir = lambda iid: hecho["destruidas"].append(iid)
    mod.esperar_estado = lambda iid, t: {"actual_status": "running", "ssh_host": "h", "ssh_port": 1}
    mod.esperar_ssh = lambda h, p: True
    mod.subir_trabajo = lambda h, p, tar, huella: None
    mod.ssh_script = lambda h, p, s, timeout: 0

    def correr_remoto(h, p, run, hasta, libro):
        if falla_run:
            raise RuntimeError("el trabajo revento")
        libro.paso("corriendo")
        return 0
    mod.correr_remoto = correr_remoto

    def traer(desc, t, h, p, apartado):
        if falla_traer:
            raise RuntimeError("no se pudo traer")
        return [], []
    mod.traer = traer
    return hecho


def esqueleto(mod, libro_dir: Path, tid="a"):
    mod.Libro(libro_dir / f"{tid}.json").paso(
        "pendiente", id=tid, etiqueta=f"t-{tid}", oferta=11, precio_hora=0.05, tope_precio=0.1)


def test_destruye_aunque_falle_el_trabajo():
    fallos = []
    for que in ("run", "traer"):
        mod = cargar()
        with tempfile.TemporaryDirectory() as d:
            f, libro = montar(Path(d))
            hecho = maquina_de_mentira(mod, falla_run=que == "run", falla_traer=que == "traer")
            esqueleto(mod, libro)
            desc = mod.cargar_descriptor(str(f))
            mod.correr_un_trabajo(desc, desc["trabajos"][0], "t-", libro, 2.0)
            lb = json.loads((libro / "a.json").read_text())
            if hecho["destruidas"] != [777]:
                fallos.append(f"falla {que}: destruidas = {hecho['destruidas']}")
            if lb["estado"] != "destruida":
                fallos.append(f"falla {que}: el libro acaba en {lb['estado']}")
            if not lb.get("error"):
                fallos.append(f"falla {que}: el libro no dice que fallo")
    return fallos


def test_el_hijo_sale_con_cero_pase_lo_que_pase():
    mod = cargar()
    with tempfile.TemporaryDirectory() as d:
        f, libro = montar(Path(d))
        mod.correr_un_trabajo = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))
        try:
            mod.cmd_trabajo(args(descriptor=str(f), prefijo="t-", libro=str(libro), uno="a"))
        except SystemExit as e:
            return [] if e.code == 0 else [f"salio con {e.code}"]
        return ["no salio con SystemExit(0)"]


def test_el_seco_no_toca_la_api():
    mod = cargar()
    sin_api(mod)
    mod.unidad_activa = lambda n: False
    with tempfile.TemporaryDirectory() as d:
        f, libro = montar(Path(d))
        try:
            mod.cmd_trabajo(args(descriptor=str(f), prefijo="t-", libro=str(libro), seco=True))
        except AssertionError as e:
            return [f"el seco toco la API: {e}"]
        return [] if not libro.exists() else ["el seco escribio libro"]


def test_sin_prefijo_se_niega_sin_tocar_la_api():
    mod = cargar()
    sin_api(mod)
    mod.die = _die
    with tempfile.TemporaryDirectory() as d:
        f, libro = montar(Path(d))
        try:
            mod.cmd_trabajo(args(descriptor=str(f), libro=str(libro)))
        except Muerte as e:
            return [] if "prefijo" in str(e) else [f"se nego por otra cosa: {e}"]
        except AssertionError as e:
            return [f"toco la API antes de negarse: {e}"]
        return ["no se nego"]


def test_un_libro_vivo_no_se_relanza():
    mod = cargar()
    sin_api(mod)
    mod.die = _die
    mod.unidad_activa = lambda n: False
    with tempfile.TemporaryDirectory() as d:
        f, libro = montar(Path(d))
        esqueleto(mod, libro)
        mod.Libro(libro / "a.json").paso("corriendo")
        try:
            mod.cmd_trabajo(args(descriptor=str(f), prefijo="t-", libro=str(libro)))
        except Muerte as e:
            return [] if "ya tiene libro" in str(e) else [f"se nego por otra cosa: {e}"]
        except AssertionError as e:
            return [f"toco la API: {e}"]
        return ["relanzo encima de un trabajo vivo"]


def test_reparte_ofertas_distintas_y_escribe_el_libro_antes():
    mod = cargar()
    mod.token = lambda: "x"
    mod.instancias = lambda: []
    mod.unidad_activa = lambda n: False
    pedidas = []

    def elegir(cuantas, *a, **k):
        pedidas.append(cuantas)
        return [{"id": 100 + i, "machine_id": 500 + i, "dph_total": 0.05} for i in range(cuantas)]
    mod.elegir_ofertas_distintas = elegir
    mod.resumen_maquina = lambda o: {"host": o["machine_id"]}
    fallos, vistos = [], []
    with tempfile.TemporaryDirectory() as d:
        f, libro = montar(Path(d))

        def lanzar(nombre, orden):
            # cuando se lanza la PRIMERA unidad, los dos libros ya tienen que existir
            vistos.append(sorted(p.name for p in libro.glob("*.json")))
            return 0
        mod.lanzar_unidad = lanzar
        mod.cmd_trabajo(args(descriptor=str(f), prefijo="t-", libro=str(libro)))
        if pedidas != [2]:
            fallos.append(f"pidio ofertas asi: {pedidas} (una llamada por 2 trabajos)")
        ofertas = {json.loads((libro / f"{t}.json").read_text())["oferta"] for t in "ab"}
        if len(ofertas) != 2:
            fallos.append(f"dos trabajos con la misma oferta: {ofertas}")
        if not vistos or "a.json" not in vistos[0] or "b.json" not in vistos[0]:
            fallos.append(f"el libro no estaba entero antes de la primera unidad: {vistos[:1]}")
        if not (libro / "descriptor-resuelto.json").exists():
            fallos.append("los hijos no tienen descriptor resuelto")
    return fallos


def test_el_payload_no_lleva_lo_que_no_debe():
    mod = cargar()
    with tempfile.TemporaryDirectory() as d:
        f, _ = montar(Path(d))
        tar = mod.tar_de_trabajo(mod.cargar_descriptor(str(f)))
        with tarfile.open(tar) as t:
            nombres = t.getnames()
    malos = [n for n in nombres if any(x in n.split("/") for x in (".git", ".venv", "datos", ".env"))]
    fallos = [f"viajaria: {malos}"] if malos else []
    if "repo/exp/nn/entrenar.py" not in nombres:
        fallos.append(f"no viaja lo que si debe: {nombres}")
    return fallos


def test_lo_traido_no_pisa_lo_local():
    mod = cargar()
    fallos = []
    with tempfile.TemporaryDirectory() as d:
        f, libro = montar(Path(d))
        desc = mod.cargar_descriptor(str(f))
        local_b = Path(d) / "repo" / "exp" / "nn" / "pesos" / "b"
        local_b.mkdir(parents=True)
        (local_b / "best.pt").write_text("EL DE AQUI\n")
        # un tar como el que mandaria la maquina: run.log y los pesos de a y de b
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as t:
            for nombre, txt in (("run.log", "log"), ("repo/exp/nn/pesos/a/best.pt", "A REMOTO"),
                                ("repo/exp/nn/pesos/b/best.pt", "B REMOTO")):
                data = txt.encode()
                info = tarfile.TarInfo(nombre); info.size = len(data)
                t.addfile(info, io.BytesIO(data))
        mod.ssh_capture = lambda h, p, s, timeout: (0, "repo/exp/nn/pesos/a\nrepo/exp/nn/pesos/b\n")

        class P:
            returncode = 0
        def run(cmd, stdout=None, timeout=None, **k):
            stdout.write(buf.getvalue())
            return P()
        mod.subprocess.run = run
        t = {"id": "x", "run": "x", "trae": ["repo/exp/nn/pesos/a", "repo/exp/nn/pesos/b"]}
        traidos, apartados = mod.traer(desc, t, "h", 1, libro / "x")
        if traidos != ["repo/exp/nn/pesos/a"]:
            fallos.append(f"traidos = {traidos}")
        if apartados != ["repo/exp/nn/pesos/b"]:
            fallos.append(f"apartados = {apartados}")
        if (local_b / "best.pt").read_text() != "EL DE AQUI\n":
            fallos.append("PISO el fichero local")
        if not (libro / "x" / "traido" / "repo/exp/nn/pesos/b/best.pt").exists():
            fallos.append("lo apartado no esta en el libro")
        if (Path(d) / "repo/exp/nn/pesos/a/best.pt").read_text() != "A REMOTO":
            fallos.append("lo que no pisaba nada no llego a su sitio")
    return fallos


def test_run_y_trae_son_relativos_al_descriptor():
    """El descriptor vive DENTRO del repo que sube y nunca nombra su carpeta: `run`
    corre donde cae el descriptor en la maquina, y `trae` se lee desde ahi."""
    mod = cargar()
    mod.die = _die
    fallos = []
    with tempfile.TemporaryDirectory() as d:
        repo = Path(d) / "repo"
        (repo / "2026-01-01-algo" / "nn").mkdir(parents=True)
        desc = {"experimento": "x", "maquina": {}, "install": "true",
                "envia": [{"origen": "../..", "destino": "repo"}],
                "trabajos": [{"id": "a", "run": "echo", "trae": ["../resultados/r1"]}]}
        f = repo / "2026-01-01-algo" / "nn" / "vast.json"
        f.write_text(json.dumps(desc))
        dd = mod.cargar_descriptor(str(f))
        if dd["base_remota"] != "repo/2026-01-01-algo/nn":
            fallos.append(f"base remota = {dd['base_remota']}")
        if mod.remota(dd, "../resultados/r1") != "repo/2026-01-01-algo/resultados/r1":
            fallos.append(f"trae se resuelve a {mod.remota(dd, '../resultados/r1')}")
        local = mod.destino_local(dd, mod.remota(dd, "../resultados/r1"))
        if local != repo / "2026-01-01-algo" / "resultados" / "r1":
            fallos.append(f"vuelve a {local}")
        desc["trabajos"][0]["trae"] = ["../../../../etc"]
        f.write_text(json.dumps(desc))
        try:
            mod.cargar_descriptor(str(f))
            fallos.append("un `trae` que sale de la raiz remota no se nego")
        except Muerte:
            pass
    return fallos


def main():
    pruebas = [
        ("se destruye aunque falle el trabajo o la recogida", test_destruye_aunque_falle_el_trabajo),
        ("el hijo sale con 0 pase lo que pase", test_el_hijo_sale_con_cero_pase_lo_que_pase),
        ("el seco no toca la API", test_el_seco_no_toca_la_api),
        ("sin --prefijo se niega sin tocar la API", test_sin_prefijo_se_niega_sin_tocar_la_api),
        ("un trabajo con libro vivo no se relanza", test_un_libro_vivo_no_se_relanza),
        ("ofertas distintas, y el libro antes de lanzar",
         test_reparte_ofertas_distintas_y_escribe_el_libro_antes),
        ("el payload no lleva .git/.venv/datos/.env", test_el_payload_no_lleva_lo_que_no_debe),
        ("lo traido no pisa lo local", test_lo_traido_no_pisa_lo_local),
        ("run y trae son relativos al descriptor", test_run_y_trae_son_relativos_al_descriptor),
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
