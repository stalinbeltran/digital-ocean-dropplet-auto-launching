# El preflight del llavero — qué comprueba de verdad, y el falso pendiente del 2026-09-10

**Estado: IMPLEMENTADO.** Este fichero nació el 2026-09-10 (`89cf343`) diciendo lo
contrario —«`launch` no comprueba el llavero»— y estaba equivocado. Se reescribe en vez
de borrarse porque el error de lectura que lo produjo es reproducible, y la mitad útil de
lo que decía sigue abierta.

## Lo que ya hace, y dónde

`comprobar_llavero()` ([`do_droplet.py:2298`](../scripts/do_droplet.py#L2298)), llamado
desde `cmd_launch` en la [línea 1060](../scripts/do_droplet.py#L1060) — **antes** de
`comprobar_size`, del volumen y del `POST /v2/droplets`. También lo llaman `provision`
([2696](../scripts/do_droplet.py#L2696)) y `push-secret --llavero`
([3469](../scripts/do_droplet.py#L3469)).

Punto por punto, contra lo que el documento original pedía como P1:

| lo que se pedía | estado |
|---|---|
| corre **antes** de crear la máquina | sí — no se ha creado ni tocado nada cuando muere |
| **se niega**, no avisa | sí — `die()`, y el docstring explica por qué muere en vez de avisar |
| **las nombra** | sí — nombre y el `porque` de cada una |
| apunta al remedio **fuera** de la máquina | sí — `secretos-desde-cero.md` y `llavero traer <maquina>` |
| distingue «falta» de «vacía» | sí — `os.environ.get(n, "").strip()`: la cadena vacía cuenta como ausente |

Y P2 —separar obligatorias de opcionales— también está hecho: cada entrada de
[`llavero.json`](../llavero.json) lleva `obligatoria` y `porque`, y `cargar_llavero()`
la pone a `False` por defecto ([2284](../scripts/do_droplet.py#L2284)).

La salida de emergencia es `--sin-llavero`, que degrada la muerte a aviso nombrando lo
que falta.

## Lo que sigue abierto (el P3 original, y es real)

**`DO_SSH_USER` está en el llavero como opcional, y su defecto lo hereda de `cfg()`:
`"root"` ([`do_droplet.py:53`](../scripts/do_droplet.py#L53)).** Su propio `porque` en
`llavero.json` empieza diciendo «NO es un secreto», y va ahí porque fue lo único del mini
viejo que se perdió al rehacerlo con efecto real.

Hay que decidir **una** de estas dos, y anotarlo aquí:

1. **Lo aporta el tipo**, como los otros ocho campos que `types/*.json` ya aporta
   (`size`, `image`, `region`, `tag`, `repos`, `services`, …). Es lo coherente: no es un
   secreto, así que no tiene por qué viajar por el canal de los secretos.
2. **El preflight lo exige** subiéndolo a `obligatoria: true`.

**Alcance real del fallo, para no sobredimensionarlo.** Sin `DO_SSH_USER`, lo único que
cambia es el atajo `do_droplet.py ssh <maquina>`, que entra como `root`. Todo lo que
aprovisiona va por `DO_DEV_USER` (defecto `deploy`,
[línea 60](../scripts/do_droplet.py#L60)) — `remoto`, `llavero`, `flota`,
`_mandar_clave_flota` — y **`provision` entra siempre como root pase lo que pase con
`DO_SSH_USER`** ([1654](../scripts/do_droplet.py#L1654)). Una máquina que nace sin ella
no nace coja: nace con un atajo incómodo.

## Lo que el preflight NO cubre, a propósito

**`--dry-run` se lo salta**, junto con el de GitHub ([1054](../scripts/do_droplet.py#L1054)):
no crea nada, así que no hay nada que proteger. Consecuencia práctica: **`--dry-run` no
sirve para probar el preflight**. El documento original proponía comprobarlo con
`launch prueba --type mini --seco`, que además falla por otra razón — esa opción no
existe, se llama `--dry-run`.

Para verlo actuar hay que llamar a la función directamente:

```bash
cd ~/src/digital-ocean-dropplet-auto-launching
python3 - <<'PY'
import os, sys
sys.path.insert(0, "scripts")
os.environ.pop("TGL_BOT_TOKEN", None)          # una OBLIGATORIA
import do_droplet
do_droplet.comprobar_llavero()                 # -> muere nombrandola
PY
```

## El falso pendiente: qué se leyó mal

El 2026-09-10, al pedirle a un dev que rehiciera el mini, se paró y dio dos motivos.
**Los dos eran falsos**, y los dos por leer mal algo que el repo sí dice bien:

1. **«El preflight falló.»** Las cinco variables que listó —`DO_SSH_USER`,
   `TGL_CLAUDE_PERMISSION_MODE`, `FVW_WEB_TOKEN`, `TGL2_BOT_TOKEN`,
   `TGL2_ALLOWED_USER_IDS`— son **todas `obligatoria: false`**. El preflight no habría
   dicho nada. `flota` imprime las dos listas por separado y con distinta grafía
   (`FALTAN obligatorias:` frente a `faltan opcionales:`,
   [4099-4103](../scripts/do_droplet.py#L4099-L4103)) precisamente para que no se
   confundan; se aplanaron las dos en un ❌ y una línea informativa se leyó como un
   bloqueo.

2. **«No podría entrar a arreglarlo.»** Citó una frase de `types/mini.json` que está en
   **pasado**, dentro de un párrafo que empieza «Hasta entonces esto era…». Desde el
   2026-09-10 las dos máquinas llevan la **clave de flota** y un dev sí entra en el mini
   — es literalmente lo que ese cambio arregló. O sea que **se alegó como motivo para no
   actuar justo la avería que el cambio elimina**.

Las dos correcciones que salen de aquí:

- **Un preflight que no distingue obligatorio de opcional en su SALIDA no sirve**, aunque
  lo distinga por dentro. Quien lee un ❌ no va a `llavero.json` a mirar el campo.
- **Una nota que mezcla presente e historia se cita en el tiempo verbal equivocado.**
  `types/mini.json` y `types/dev.json` se reescribieron para poner el estado de hoy
  primero y la historia al final, marcada («hasta el 2026-09-10 y NO DESPUÉS») y con el
  aviso de comprobar antes de citarla.

## Lo que NO hay que hacer

⚠ **No implementar `comprobar_llavero()` otra vez.** Es el riesgo concreto que este
documento creaba mientras decía «no existe»: un segundo escritor de la misma
comprobación diverge del primero, y el que se depura después es siempre el que no
escribiste tú. Es la misma razón por la que `install-service` reusa
`build_service_section` en vez de generar la unidad por su cuenta.

⚠ **No rellenar el llavero de un dev a mano y dar el problema por resuelto.** Eso arregla
esa máquina, y estas máquinas se destruyen. Lo que hay que arreglar está en
`llavero.json` y en los tipos, que es lo que viaja.
