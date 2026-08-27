# El reparto: qué va en el mini y qué va en dev

**Fecha:** 2026-08-23. **Estado:** implementado (`types/mini.json`, `types/dev.json`).

Dos máquinas, y una sola idea detrás: **una sobrevive y la otra se tira**. Todo lo
demás sale de ahí, incluida la regla nueva de los tokens.

| | **mini** | **dev** |
|---|---|---|
| para qué | lanzar la de trabajo, saber qué hay vivo, apagarlo | todo el trabajo: Claude Code y las peticiones complejas |
| vida | siempre encendida, tag `control` | desechable, tag `ephemeral` |
| tamaño | 512 MB (`s-1vcpu-512mb-10gb`) | 2 vCPU / 4 GB |
| Claude Code | **no** — no cabe en 512 MB | sí |
| bot | Lanzador (`TGL_`) | Coordinador (`TG_`) |
| repos de trabajo | ninguno | foveal-vision, foveal-vision-**data**, image-text-sample-generator |
| alquila en Vast | **no** | sí |
| **apaga** en Vast | **sí** | sí |

Esa última fila es la que no es obvia, y es la regla nueva.

---

## La regla: el superviviente tiene que poder apagar todo

> **El token de cualquier cosa que dev pueda ENCENDER tiene que estar también en
> el mini. No para encender: para apagar.**

El razonamiento es corto y no admite excepción: dev alquila máquinas en Vast que
facturan por segundo, y dev es desechable. Si dev muere —lo destruyes tú, se
queda sin disco, lo que sea— **el mini es lo único que queda capaz de enumerar y
matar lo que dev dejó encendido**. Un `apagar-vast` sin `VAST_AI_API_TOKEN` en el
mini es un botón que no hace nada, y el síntoma es una factura.

Por eso `types/mini.json` declara `push_env: ["VAST_AI_API_TOKEN"]` aunque desde
el mini no se alquile nada nunca. Y por eso, si algún día dev puede encender algo
en un proveedor nuevo, **ese token entra en el mini en el mismo commit**. Es la
misma forma que la regla de «el freno nunca llega después del acelerador», pero
aplicada a las máquinas en vez de a los comandos.

## Un secreto tiene dos destinos, y el ancho es `push_env`

Esto importa al escribir un tipo, y es fácil equivocarse porque los nombres no lo
sugieren:

| mecanismo | dónde escribe | quién lo ve |
|---|---|---|
| **`push_env`** del tipo (o `push-secret`) | `~/.config/dev-secrets.env` | **el bot Y las sesiones SSH** |
| **`env_prefix`** del servicio (o `push-service-env`) | el `.env` del repo del servicio | **sólo el bot** |

El ancho es `push_env`, y el mecanismo es el que explica por qué: el unit de
systemd arranca con `ExecStart=/bin/bash -lc`, y `provision` mete la línea que
carga `dev-secrets.env` **al principio** de `.bashrc` —antes del guard de «si no
es interactiva, no hagas nada»—, así que el proceso del bot también lo lee. La
trampa documentada en 2026-08-20 es la dirección contraria: un token puesto sólo
en el `.env` del servicio funciona desde Telegram y falla entrando por SSH a la
misma máquina.

**Regla práctica:** un token que use *una herramienta* (`do_droplet.py`,
`vast_instance.py`, `gh`) va por `push_env`. Uno que sea *configuración del
servicio* (`BOT_TOKEN`, `ALLOWED_USER_IDS`) va por el puente `env_prefix`.

## El mini es también el llavero

No es sólo el superviviente: es **quien provisiona a dev**, así que los secretos
de dev salen de él. `GITHUB_TOKEN` es el que más se olvida y no aparece en ningún
tipo, porque `provision` lo propaga solo si la máquina que lanza lo tiene. Sin él:

- dev clona igual (estos repos son públicos), así que **no se nota al crearla**;
- y dev **no puede empujar**, que es lo único que salva su trabajo cuando la
  destruyas. El fallo aparece horas después, al final del encargo.

Comprobarlo en el mini es una línea: `shell` → `echo ${GITHUB_TOKEN:+puesto}`.

## El día a día

```
lanzar   launch dev          (al Lanzador)   →  ~5 min, y dev arranca con su bot
…trabajas hablándole al Coordinador…
estado                       (al Lanzador)   →  qué hay vivo en las dos nubes
lanzar   destroy dev --yes   (al Lanzador)
```

`types/dev.json` trae dentro los repos, el servicio, `make_launcher` y el
`register-key` de Vast, así que **el lanzamiento cabe en un mensaje** y no hay
que recordar la versión larga — que es la clase de cosa que se teclea mal desde
el móvil y crea una máquina que factura y no sirve.

⚠ **Y por eso lo que tiene que estar siempre va en el TIPO, nunca en el comando.**
`foveal-vision-data` —donde se guarda todo lo que se mide— faltaba en la lista, y
lo que pasa entonces no se ve: `fv.settings.data_root()` cae al repo de código,
donde `runs/` y `sweeps/` están en `.gitignore`, así que un estudio corre entero,
escribe sus resultados y no los commitea en ninguna parte. Ni un error. Medido el
2026-08-27 en un dev recién rehecho. La regla que deja: **si para que algo se
guarde hay que acordarse de un `--repo`, tarde o temprano no se guarda.**

⚠ Un tipo que cambia sólo llega a las máquinas que se creen DESPUÉS, y sólo si el
mini tiene el repo del lanzador al día: es él quien lee `types/dev.json` al
lanzar. Tras tocar un tipo, `actualizar` en el Lanzador (un `git pull` en todos
sus repos) y ya. El mini no necesita el repo de datos —no mide nada—, necesita
saber que dev sí.

Si algo se cortó a mitad y quedó algo encendido: `apagar-vast` (todas las de
Vast) y `apagar-do` (los droplets `ephemeral`, **nunca el mini**).

> ⚠️ `apagar-do` **también destruye dev**, que lleva tag `ephemeral` a propósito:
> es lo que lo hace desechable. Para matar sólo una de varias, por nombre:
> `lanzar destroy dev-02 --yes`. Si prefieres que dev sobreviva a los barridos,
> es una línea en `types/dev.json` (`"tag": "trabajo"`), pero entonces destruirla
> es siempre por nombre.

## Reemplazar el mini: cuidado con el 409

Lanzar un mini nuevo mientras el viejo vive **no funciona con el mismo bot**:
Telegram sólo admite un proceso haciendo long polling por token, y el segundo se
queda fuera con un `409`. O sea que te quedas sin mando en una de las dos.

Tres salidas, de mejor a peor:

1. **Un bot de staging** (`TGL2_BOT_TOKEN`, otro `/newbot`). Las dos vivas, dos
   chats, compruebas y destruyes la vieja. Es lo que recomendaría.
2. **Nacer sin servicio** (`--service ''`) y verificar desde `post`, avisando con
   `notify.mjs`. Detalle útil: **el 409 es del `getUpdates`, no del
   `sendMessage`** —`notify.mjs` ya envía con el mismo token mientras el bot
   hace polling—, así que la máquina nueva puede reportarte a tu chat de siempre
   sin robarte el bot.
3. Parar el bot viejo antes de arrancar el nuevo. **No**: si el nuevo falla te
   quedas sin ninguno y necesitas la laptop, que es justo lo que el mini evita.

## Las condiciones de Vast son dato: `vast-perfiles/`

En Vast no se pide una máquina por su nombre: se **busca**, con un rango de vCPU,
un mínimo de RAM y un tope de precio. Esas condiciones se aprenden pagando, así
que se guardan:

```
vast-perfiles/<nombre>.json     cpus, max_cpus, min_ram, max_price, bench,
                                horas_max, disk, image, descripcion, notas
```

```sh
python3 scripts/vast_instance.py perfiles                    # qué hay
python3 scripts/vast_instance.py sweep --perfil foveal-cpu   # medir con ellas
python3 scripts/vast_instance.py sweep --perfil foveal-cpu --cpus 4   # y pisar una
```

Lo explícito manda sobre el perfil, y el perfil sobre el default — igual que
`types/` en DigitalOcean. Lo aceptan `offers`, `launch`, `bench` y `sweep`.

`vast-perfiles/foveal-cpu.json` lleva dentro lo que costó descubrirlo: el
`min_ram: 8` está ahí porque `sweep` coge siempre la oferta más barata del rango,
y el 2026-08-21 dos intentos seguidos cayeron en la **misma** oferta rota (una
rebotando la clave SSH, otra con un sshd mudo). Se sale estrechando la búsqueda,
no cableando una lista de ofertas prohibidas — y ahora eso es un fichero y no una
frase en un README que nadie relee.

## Lo que NO va en el mini

Repos de trabajo, el dataset, el volumen, Claude Code. En 512 MB no caben y no
hay nada que hacer con ellos: el mini no mide, no entrena y no conversa.

**Hueco conocido**, que sale de la federación de ejecutores: el mini ofrece `c` y
`creset` en `/executors` y **fallan**, porque vienen de `data/executors/` del
repo del coordinador —que siempre está ahí, es el propio servicio— pero `claude`
no está instalado. La federación ata un comando a un **repo**; `c` depende de un
**binario**. La salida barata sería un campo `requiere: ["claude"]` en el JSON del
ejecutor, y que `/executors` lo marque como no disponible en vez de ofrecerlo. No
está implementado.
