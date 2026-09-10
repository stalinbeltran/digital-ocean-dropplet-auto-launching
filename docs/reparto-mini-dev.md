# Los ROLES: qué hace el mini y qué hace dev

**Fecha:** 2026-08-23. **Actualizado:** 2026-09-10.
**Estado:** describe los ROLES. La parte de **privilegios quedó superada** por
[`flota-simetrica.md`](flota-simetrica.md), implementado y probado ese día.

> ⚠ **Este documento ya NO dice quién puede qué.** Desde el 2026-09-10 mini y dev pueden
> exactamente lo mismo: mismo llavero, misma clave de flota, y cada una crea, repara y
> destruye a la otra — **comprobado destruyendo el mini de verdad desde un dev y
> volviéndolo a crear**. Lo que sigue describe para qué se usa cada una, que es otra cosa.

Dos máquinas, una sola idea: **una está siempre encendida y la otra se tira**. Ojo con la
diferencia respecto de antes, porque es toda la diferencia: es **disponibilidad**, no
permisos.

| | **mini** | **dev** |
|---|---|---|
| para qué se usa | pedirle máquinas: lanzar, ver qué hay vivo, apagarlo | el trabajo: Claude Code y las peticiones complejas |
| vida | siempre encendida, tag `control` | desechable, tag `ephemeral` |
| tamaño | 512 MB (`s-1vcpu-512mb-10gb`), 4 $/mes | 2 vCPU / 4 GB, 24 $/mes |
| Claude Code | **no** — en 512 MB lo mata el kernel | sí |
| bot | Lanzador (`TGL_`) | Coordinador (`TG_`) |
| **llavero, repos, clave de flota, crear y destruir** | **iguales** | **iguales** |

Las **tres** diferencias que quedan, y el motivo de cada una:

1. **La talla**, de donde sale lo de Claude Code. Es la única que el dueño aceptó como
   permanente.
2. **El tag**, que decide qué se lleva `apagar-do`. Es una etiqueta de barrido.
3. **El bot**, que **no es opcional**: Telegram sólo admite un proceso haciendo long
   polling por token y el segundo recibe un **409**. Con un solo bot, una de las dos se
   queda muda.

---

## La regla vieja, y qué la sustituye

Aquí decía: *«el token de cualquier cosa que dev pueda ENCENDER tiene que estar también
en el mini. No para encender: para apagar»*. Sigue siendo cierto, pero **se quedaba
corta**, y lo que faltaba costó descubrirlo:

> **La regla de hoy: si una variable hace falta para CREAR una máquina de la flota, va en
> [`llavero.json`](../llavero.json).**

La vieja cubría los tokens que encienden algo. No cubría los que no encienden nada y sin
los cuales la máquina **nace coja**. Medido el 2026-09-10: el mini llevaba `TG_*` y no
`TGL_*`, o sea que sabía parir un dev y **no sabía parir un mini**; y de sus 19 variables,
`types/mini.json` declaraba **una** — las demás se habían empujado a mano, así que el mini
no se reconstruía desde el repo.

El caso de Vast sigue valiendo como ejemplo y ahora es una consecuencia, no una regla
aparte: `VAST_AI_API_TOKEN` es obligatorio en el llavero, así que lo llevan las dos.

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

## Rehacer el mini: probado, y cuesta una IP

**Comprobado el 2026-09-10**: un dev destruyó el mini con `destroy mini --yes` y lo volvió
a crear con `launch mini --type mini`, con su llavero, sus repos y su bot. Comparado con la
foto tomada antes: **ningún secreto se perdió**. De las 24 variables que tenía, 9 no
viajaron, y 8 de esas las aporta ahora el tipo (repos, size, image, region, tag, servicios).

⚠ **Lo que sí cambia es la IP** — medido: de `67.205.158.85` a `159.89.83.184`. Todo lo que
apunte a la vieja deja de resolver.

⚠ **Y la novena variable enseñó algo**: `DO_SSH_USER` no viajaba y no la aportaba el tipo,
así que el mini renacido caía al default `root`. El síntoma habría sido el de siempre —
`ssh` entrando como root, sin `dev-secrets.env` en su home, y un «falta el token» en una
máquina donde el token sí está. Está en el llavero desde entonces, aunque no sea un
secreto: **el llavero es «lo que hace falta para que la máquina nazca entera», no sólo
«lo que hay que esconder»**.

## Reemplazar el mini estando el viejo vivo: cuidado con el 409

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

Sólo **Claude Code**, y el volumen de bloques. Los repos de trabajo **sí van** desde el
2026-09-10, tras medirlo: los siete pesan 365,7 MB clonados contra 4,4 GB libres de los
8,7 del disco. Si algún día `foveal-vision-data` (334 MB, el que crece) apretara, **ésa es
la primera línea que se corta** — el mini no mide nada — y se anota aquí como excepción en
vez de dejarlo en una conversación.

**Hueco conocido**, que sale de la federación de ejecutores: el mini ofrece `c` y
`creset` en `/executors` y **fallan**, porque vienen de `data/executors/` del
repo del coordinador —que siempre está ahí, es el propio servicio— pero `claude`
no está instalado. La federación ata un comando a un **repo**; `c` depende de un
**binario**. La salida barata sería un campo `requiere: ["claude"]` en el JSON del
ejecutor, y que `/executors` lo marque como no disponible en vez de ofrecerlo. No
está implementado.
