# Flota simétrica: que mini y dev sean la misma máquina con dos tallas

**Fecha:** 2026-09-10. **Estado:** diseño, sin implementar.
**Sustituye a** la parte de «privilegios» de [`reparto-mini-dev.md`](reparto-mini-dev.md),
que queda como descripción del reparto de *roles*.

---

## 1. Qué está mal hoy

El reparto actual no es sólo «una sobrevive y la otra se tira»: es **una manda y la otra
obedece**. El mini tiene el llavero, el mini crea al dev, y el dev no puede devolverle
nada. Las consecuencias, medidas hoy (2026-09-10) contra las máquinas vivas:

### 1.1 El dev no puede entrar en el mini. Nunca ha podido.

```
$ python scripts/do_droplet.py ssh mini --cmd "sudo awk '{print $1, $3}' /root/.ssh/authorized_keys"
ssh-ed25519 do-droplet-20260811
ssh-ed25519 do-droplet
ssh-ed25519 do-droplet
```

Tres claves: las dos laptops y la del propio mini. **Ninguna `lanzador-dev`.** Y no es un
olvido, es aritmética del orden en que nacen las cosas: un droplet acepta las claves que
están registradas en la cuenta **en el momento de crearlo**, y la clave del dev se genera
y se registra durante el `provision` del dev, que es siempre *después* de que el mini
exista. El mini entra en todo lo que crea; nada de lo que crea entra en el mini.

`authorize-key` existe para taparlo a mano, pero está a medias para este uso:
`cmd_authorize_key` ([do_droplet.py:3103](../scripts/do_droplet.py#L3103)) escribe en
`Path.home()/.ssh/authorized_keys` —el home de **quien lo ejecuta**, que desde el bot es
`deploy`— y en cambio `run_remote_script`
([do_droplet.py:1426](../scripts/do_droplet.py#L1426)) entra **siempre como root**. O sea
que autorizar desde Telegram deja al dev entrando como `deploy` y sin poder usar
`push-secret`, `push-github-token`, `push-dir` ni `provision`, que son justo los que
importan.

### 1.2 Ninguna de las dos máquinas puede parir un mini.

`service_env_lines()` ([do_droplet.py:1678](../scripts/do_droplet.py#L1678)) construye el
`.env` del servicio copiando del entorno **de la máquina que lanza** las variables con el
prefijo del servicio. Para que un mini nuevo arranque con su bot hacen falta
`TGL_BOT_TOKEN` y `TGL_ALLOWED_USER_IDS` en quien lo lanza. Medido hoy en el mini vivo:

```
$ ... grep -o '^export [A-Z_]*' ~deploy/.config/dev-secrets.env
CLAUDE_CODE_OAUTH_TOKEN  DO_DEV_USER  DO_IMAGE  DO_REGION  DO_REPOS  DO_SERVICES
DO_SIZE  DO_SSH_PORTS  DO_SSH_USER  DO_TAG  DO_TOKEN  GH_TOKEN  GITHUB_TOKEN
GIT_USER_EMAIL  GIT_USER_NAME  TG_ALLOWED_USER_IDS  TG_BOT_TOKEN
TG_CLAUDE_PERMISSION_MODE  VAST_AI_API_TOKEN
```

**Hay `TG_*` y no hay `TGL_*`.** El mini sabe parir un dev y no sabe parir un mini: si se
pierde, se rehace sólo desde la laptop. Y el dev no tiene ni una cosa ni la otra:
`types/dev.json` declara `push_env: ["VAST_AI_API_TOKEN"]` y nada más, así que un dev no
puede parir *ninguna* de las dos con su bot puesto.

### 1.3 El llavero del mini no está escrito en ninguna parte.

Esas 19 variables no salen de `types/mini.json`, que declara **una**
(`VAST_AI_API_TOKEN`). Las demás llegaron por `push-secret` a mano, en algún momento, por
alguien. O sea que **el mini de hoy no se puede reconstruir desde el repo**: rehacerlo da
una máquina que parece igual y no lo es, y la diferencia sólo se nota cuando algo falla
semanas después. Es exactamente la avería del 2026-09-06 (`GITHUB_TOKEN` revocado)
entrando por otra puerta: lo que no se declara, no se comprueba.

### 1.4 De regalo: 26 claves muertas en la cuenta.

```
26  lanzador-dev        1  mini        1  lanzador-bench-control
 1  foveal              1  DESKTOP-RLVMP0N            1  AIG0009690-do-droplet
```

`hacer_lanzador()` ([do_droplet.py:1480](../scripts/do_droplet.py#L1480)) genera un par
nuevo **en cada dev** y lo registra en la cuenta. Los dev se destruyen; las claves se
quedan. Con `DO_SSH_KEYS=` (vacío = todas), cada droplet nuevo nace con las 31 dentro, 26
de ellas de máquinas que ya no existen.

---

## 2. La idea, en una frase

> **mini y dev dejan de ser dos clases de máquina y pasan a ser dos tallas de la misma.
> Cualquiera de las dos puede crear, reparar, actualizar y destruir a la otra y a sí
> misma. La única diferencia es que en el mini no se instala Claude Code.**

Lo que hoy es herencia accidental —«tiene lo que tenía quien la creó»— pasa a ser dato
declarado: un **llavero** que se escribe una vez y se comprueba en cada lanzamiento.

---

## 3. Los pasos grandes

| # | Paso | Qué arregla | Toca |
|---|---|---|---|
| 1 | **El llavero se declara** en un fichero, igual para las dos | 1.2, 1.3 | `llavero.json`, `types/`, `build_provision_script` |
| 2 | **Una clave de flota** en vez de una por máquina | 1.1, 1.4 | `hacer_lanzador`, `authorize-key`, comando `clave-flota` |
| 3 | **Los comandos «de dentro» se piden desde fuera** (`remoto`) | 1.1 | comando `remoto` + ejecutor de Telegram |
| 4 | **El rol queda reducido a talla + bot**; el resto se iguala | paridad | `types/mini.json`, `types/dev.json` |
| 5 | **Rehacer cualquiera de las dos** deja de necesitar la laptop | irremplazabilidad | `TGL2_`, procedimiento |
| 6 | **La paridad se comprueba con un comando**, no con fe | regresiones | comando `flota` |
| 7 | **La documentación dice la regla nueva** | que no se deshaga | `CLAUDE.md`, `docs/`, `README.md` |

El orden importa: **2 antes que 3** (sin acceso no hay comando remoto), **1 antes que 4**
(sin llavero declarado, igualar los tipos no iguala nada), y **6 antes de dar nada por
bueno**.

---

## 4. Los pasos, en detalle

### Paso 1 — El llavero es dato, no herencia

**Problema exacto.** Hoy lo que una máquina lleva es la unión de tres cosas que no se
miran juntas en ningún sitio:

| de dónde sale | qué mete | dónde está escrito |
|---|---|---|
| `build_provision_script()` a pelo | `CLAUDE_CODE_OAUTH_TOKEN`, `GITHUB_TOKEN`, `GH_TOKEN`, `DO_TOKEN` | en el código |
| `push_env` del tipo | lo que liste el tipo | `types/*.json` |
| `env_prefix` del servicio | lo que empiece por `TG_` / `TGL_` **en el entorno de quien lanza** | `services/*.json` |

La tercera es la traidora: no declara nombres, **barre el entorno**. Si la variable no
está, no falla — escribe un `AVISO: no hay ninguna variable TGL_*` entre cien líneas y
sigue. Es la misma forma del fallo del 2026-09-06.

**Diseño.** Un fichero nuevo en la raíz, `llavero.json`, con la lista completa de lo que
una máquina de la flota tiene que llevar. Dato, no código, como `types/` y `services/`:

```jsonc
{
  "descripcion": "Lo que lleva CUALQUIER maquina de la flota (mini o dev).",
  "variables": [
    {"nombre": "DO_TOKEN",                 "obligatoria": true,
     "porque": "crear y destruir droplets; sin el no es una maquina de la flota"},
    {"nombre": "GITHUB_TOKEN",             "obligatoria": true,
     "porque": "clonar los repos privados y EMPUJAR; va a tres destinos"},
    {"nombre": "VAST_AI_API_TOKEN",        "obligatoria": true,
     "porque": "alquilar y sobre todo APAGAR en Vast"},
    {"nombre": "CLAUDE_CODE_OAUTH_TOKEN",  "obligatoria": true,
     "porque": "el mini no usa Claude, pero tiene que poder darselo al dev que cree"},
    {"nombre": "TG_BOT_TOKEN",             "obligatoria": true,
     "porque": "sin el, esta maquina no puede parir un dev con su bot"},
    {"nombre": "TG_ALLOWED_USER_IDS",      "obligatoria": true, "porque": "idem"},
    {"nombre": "TG_CLAUDE_PERMISSION_MODE","obligatoria": false,"porque": "ajuste del dev"},
    {"nombre": "TGL_BOT_TOKEN",            "obligatoria": true,
     "porque": "sin el, esta maquina no puede parir un mini con su bot"},
    {"nombre": "TGL_ALLOWED_USER_IDS",     "obligatoria": true, "porque": "idem"},
    {"nombre": "GIT_USER_NAME",            "obligatoria": true, "porque": "autoria de los commits"},
    {"nombre": "GIT_USER_EMAIL",           "obligatoria": true, "porque": "idem"},
    {"nombre": "FVW_WEB_TOKEN",            "obligatoria": false,
     "porque": "token de la web app; sobrevive a rehacer el dev"},
    {"nombre": "TGL2_BOT_TOKEN",           "obligatoria": false,
     "porque": "bot de staging, para rehacer el mini estando el mini vivo (paso 5)"}
  ]
}
```

**La regla que deja, y que es la que hay que escribir en `CLAUDE.md`:**

> **Si una variable hace falta para CREAR una máquina de la flota, va en el llavero.**
> La regla vieja —«el token de lo que dev pueda encender va también en el mini, para
> poder apagarlo»— es un caso particular de ésta, y se queda corta: no cubre las
> variables que no encienden nada pero sin las cuales la máquina nace coja.

**Cambios de código.**

1. `cargar_llavero() -> list[dict]` junto a `load_type()` / `load_service()`, mismo
   estilo: lee el JSON, valida los campos, muere con un mensaje útil si está mal.
2. `types/*.json` acepta `"llavero": true`. Lo ponen `mini.json` y `dev.json`. **No** lo
   ponen `gpu-*`, `cpu`, `big` ni `bench-control`: el objetivo 5 dice que un secreto no
   viaja a donde no hace falta, y una máquina de medir no crea nada.
3. `comprobar_llavero(sin_llavero: bool) -> dict[str, str]`, hermano de
   `comprobar_github_token()` ([do_droplet.py:1962](../scripts/do_droplet.py#L1962)) y con
   el mismo trato:
   - se llama **antes de crear nada** en `cmd_launch` y **antes de tocar la máquina** en
     `cmd_provision`;
   - si falta una `obligatoria`, **muere** listando cuáles y de dónde sacarlas. No un
     `AVISO`. El precedente está medido: un aviso entre cien líneas con `exit 0` hizo que
     un lanzamiento diera por bueno un mini roto;
   - `--sin-llavero` es la salida de emergencia, como `--sin-github`: la máquina nace
     coja, a sabiendas y dicho en voz alta.
4. `build_provision_script()` escribe el llavero en `dev-secrets.env` junto a lo que ya
   escribe. Las variables con prefijo (`TG_*`, `TGL_*`) van **también** ahí, no sólo al
   `.env` del servicio: es lo que hace que la máquina pueda pasárselas a la siguiente.
   `service_env_lines()` no cambia — sigue barriendo el entorno, y ahora encuentra lo que
   busca porque el llavero lo puso.
5. `push-secret --llavero`: manda el llavero entero a una máquina viva, sin borrar lo
   demás. Es el camino de reparación para el mini de hoy, que se arregla sin rehacerlo.

**Cómo se comprueba.** `launch prueba --type dev --dry-run` desde una máquina a la que le
falte una obligatoria tiene que morir sin crear nada, diciendo el nombre.

---

### Paso 2 — Una clave de flota

**Problema exacto.** El acceso entre máquinas depende del **orden de nacimiento**, y eso
no se arregla con más claves: se arregla con una que exista antes que todas.

**Diseño.**

```
~/.ssh/do_flota          par ed25519, generado UNA vez en la laptop
                         registrado en la cuenta como la clave `flota`
```

- Todo droplet nacido después la lleva en `root` y en `deploy`, porque `DO_SSH_KEYS=`
  vacío significa «todas las de la cuenta».
- La **privada** viaja a cada máquina de la flota igual que los tokens: por SSH, por
  stdin, a un fichero 600 del usuario de desarrollo. Y `dev-secrets.env` fija
  `DO_SSH_KEY_FILE=~/.ssh/do_flota`, para que `ssh_command()` la use sin pensar.
- `hacer_lanzador()` cambia de comportamiento: **si la clave de flota llegó, no genera par
  propio ni registra nada en la cuenta**. Se acaba el goteo de `lanzador-dev`.
- El caso «no llegó» sigue funcionando como hoy (genera y registra), para no dejar sin
  arreglo una máquina lanzada con `--sin-llavero`.

**Comandos nuevos / cambiados.**

| comando | qué hace |
|---|---|
| `clave-flota` | genera `~/.ssh/do_flota` si no existe y la registra en la cuenta como `flota`. Idempotente, como `keygen` + `register-key` |
| `authorize-key --usuario root` | hoy escribe sólo en el home de quien ejecuta. Hace falta poder autorizar **root**, porque todo el aprovisionamiento entra por ahí. Por defecto, las dos (`root` y `DO_DEV_USER`) |
| `keys --prune lanzador-*` | borra de la cuenta las claves que sobran, con `--dry-run` primero. 26 candidatas hoy |

**Arranque en frío del mini que ya existe** (no se rehace, se repara — dos órdenes desde
la laptop):

```powershell
python scripts/do_droplet.py clave-flota
python scripts/do_droplet.py ssh mini --cmd "cd ~/src/digital-ocean-dropplet-auto-launching && python3 scripts/do_droplet.py authorize-key --usuario root --usuario deploy '<contenido de do_flota.pub>'"
```

**La decisión incómoda, dicha entera.** Una clave compartida significa que quien entre en
una máquina de la flota entra en las demás. Se acepta porque **ya era así**: esa máquina
lleva `DO_TOKEN`, y con `DO_TOKEN` se puede destruir el mini, hacerle una snapshot o crear
una máquina nueva con la clave que a uno le dé la gana. La clave de flota no sube el techo
del daño; sólo lo hace utilizable para lo que queremos.

**La alternativa que se descarta**, y por qué: claves por máquina más un empujón de
`authorize-key` cruzado en cada lanzamiento. Es O(N²), necesita un registro vivo de quién
existe, y **falla justo cuando hace falta**: si el mini está caído, el dev nuevo no puede
darle su clave, que es el escenario para el que se hace todo esto.

---

### Paso 3 — Los comandos «de dentro» se piden desde fuera

**Problema exacto.** `update`, `install-service`, `install-executors` y `authorize-key`
actúan sobre la máquina donde corren; `dentro_del_droplet()`
([do_droplet.py:2272](../scripts/do_droplet.py#L2272)) se niega a ejecutarlos en la
laptop, y con razón. Pero hoy la única forma de pedírselos a **otra** máquina es teclear a
mano la versión larga:

```
python scripts/do_droplet.py ssh mini --cmd 'cd ~/src/digital-ocean-dropplet-auto-launching && python3 scripts/do_droplet.py update'
```

Que es exactamente la clase de línea que desde el móvil se teclea mal.

**Diseño.** Un comando `remoto`:

```
python scripts/do_droplet.py remoto <maquina> <comando...>
```

- Resuelve IP y puerto con `resolve_target()`, como el resto.
- Ejecuta **dentro** `python3 scripts/do_droplet.py <comando...>`, con `cd` a la raíz del
  repo del lanzador, **como `DO_DEV_USER` y con `sudo -u ... -H bash -lc`**. El shell de
  login no es adorno: sin él no se carga `dev-secrets.env` y el comando de dentro falla
  con un «falta el token» en una máquina donde el token sí está. Es el mismo mecanismo que
  ya usa `ejecutar_post()` ([do_droplet.py:1022](../scripts/do_droplet.py#L1022)), y por el
  mismo motivo.
- Devuelve el código de salida de dentro, para que se pueda encadenar y para que el bot
  publique el stderr cuando algo falla.

**Su ejecutor de Telegram, en el mismo commit** (`telegram/executors/remoto.json`), por la
convención del repo: *terminado = el comando existe Y se puede invocar desde Telegram*.
Con él, la frase que abre este documento deja de ser cierta:

```
remoto mini update                       # el dev actualiza el codigo del mini
remoto mini install-service --service X  # y le instala un servicio nuevo
remoto dev  update                       # y al reves, igual
```

---

### Paso 4 — El rol, reducido a talla y bot

Con los pasos 1-3 hechos, los dos tipos se quedan así. **Lo que no está en esta tabla es
idéntico**, y ésa es la propiedad que se quiere:

| | `types/mini.json` | `types/dev.json` |
|---|---|---|
| `size` | `s-1vcpu-512mb-10gb` | `s-2vcpu-4gb` |
| `cloud_init` | `cloud-init.mini.yaml` (sin Claude Code, con swap) | `cloud-init.yaml` |
| `tag` | `control` | `ephemeral` |
| `services` | `telegram-launcher` (`TGL_`) | `telegram-coordinator` (`TG_`), `foveal-vision-web` |
| `llavero` | `true` | `true` |
| `repos` | los mismos | los mismos |
| `make_launcher`, `post` | los mismos | los mismos |

**Los dos bots tienen que seguir siendo dos.** No es preferencia: Telegram sólo admite un
proceso haciendo long polling por token, y el segundo se queda fuera con un `409`. La
diferencia de bot es *configuración de rol*, no un privilegio, y `selected_services()`
([do_droplet.py:1653](../scripts/do_droplet.py#L1653)) ya se niega a instalar juntos dos
servicios del mismo directorio, que es la red de seguridad.

**Lo que sí hay que medir antes de igualar los repos.** El mini tiene 10 GB de disco.
Clonar `foveal-vision`, `foveal-vision-data`, `image-text-sample-generator` y
`estudios-redes-neuronales` no cuesta RAM, pero sí disco, y `foveal-vision-data` crece con
cada estudio. **Paso previo obligatorio:** medir los cuatro repos (`du -sh ~/src/*` en un
dev vivo) y comprobar que caben con holgura. Si no caben, la línea que se corta es
`foveal-vision-data` en el mini —el mini no mide nada— y **se anota aquí como la primera
excepción real**, no se deja en la conversación.

---

### Paso 5 — Rehacer cualquiera de las dos, sin la laptop

Con el llavero y la clave de flota, `launch mini2 --type mini` desde el dev crea un mini
completo. Queda un obstáculo que no es de privilegios sino de Telegram: **el 409**. Dos
minis vivos con el mismo `TGL_BOT_TOKEN` y uno de los dos se queda sin mando.

Salidas, de mejor a peor (esto ya estaba estudiado en `reparto-mini-dev.md`; aquí sólo se
fija cuál se elige):

1. **Bot de staging `TGL2_`.** Va en el llavero como opcional. `mini2` nace con
   `TGL2_BOT_TOKEN`, las dos máquinas vivas, dos chats, se comprueba y se destruye la
   vieja. **Es la elegida.**
2. Nacer sin servicio (`--service ''`) y avisar por `notify.mjs` — el `409` es del
   `getUpdates`, no del `sendMessage`, así que la máquina nueva puede escribirte al chat
   de siempre sin robarte el bot.
3. Parar el bot viejo antes de arrancar el nuevo. **No**: si el nuevo falla te quedas sin
   ninguno, que es justo lo que el mini evita.

**Y la regla de no destruir el mini cambia de motivo, no de contenido.** Hoy es «es la
única con privilegios»; a partir de este diseño es «**es la única siempre encendida**».
Sigue sin destruirse en ninguna limpieza, y sigue destruyéndose sólo por su nombre y a
propósito.

---

### Paso 6 — La paridad se comprueba con un comando

Un `do_droplet.py flota`, sólo lectura, que para cada máquina viva de la flota diga:

```
maquina   llavero        clave flota   entra en   rol
mini      13/13          si            dev        control  · telegram-launcher
dev       12/13 (falta   si            mini       trabajo  · telegram-coordinator,
          FVW_WEB_TOKEN)                                     foveal-vision-web
```

- Comprueba **presencia**, nunca valor, y no imprime ningún secreto.
- El `GITHUB_TOKEN` se comprueba **preguntándole a GitHub**, con `github_token_estado()`
  ([do_droplet.py:1921](../scripts/do_droplet.py#L1921)), que ya existe: un token revocado
  tiene el mismo aspecto que uno bueno, y eso ya costó una avería.
- Sale `!= 0` si a alguna le falta algo obligatorio, para poder encadenarlo y para que el
  bot publique el motivo.
- Es, además, el `do_check.py` hermano de `vast_check.py` que `CLAUDE.md` tiene apuntado
  como pendiente: comprobar lo que se puede hacer **antes** de gastar.

**Prueba de aceptación del diseño entero.** No se da por bueno hasta que estas cuatro
salgan, y en este orden:

```
1.  desde el mini:  launch dev  --type dev        →  dev con su bot, sus repos y su llavero
2.  desde el dev:   remoto mini update            →  el mini se actualiza solo
3.  desde el dev:   launch mini2 --type mini      →  un mini nuevo con bot TGL2_
4.  desde mini2:    launch dev2 --type dev        →  la flota se reproduce sin la laptop
    y por el camino:  flota  →  0, sin huecos
```

La 3 y la 4 son las que hoy no existen. La 4 es la que demuestra que la propiedad no
depende de la laptop.

---

### Paso 7 — Documentación

| fichero | qué cambia |
|---|---|
| `CLAUDE.md` | la regla del llavero **sustituye** a la del freno/acelerador (que queda como caso particular); la clave de flota; el motivo nuevo de no destruir el mini; `remoto` en la lista de comandos |
| `docs/reparto-mini-dev.md` | deja de hablar de privilegios y pasa a describir **roles**; enlaza aquí |
| `docs/flota-simetrica.md` | este fichero, que pasa de «diseño» a «implementado» con la fecha |
| `README.md` | `clave-flota`, `remoto`, `flota`, `push-secret --llavero`, `--sin-llavero`, y el procedimiento de rehacer el mini |
| `.env.example` | `TGL_*` y `TGL2_*` documentados como parte del llavero, con de dónde sale cada uno |

---

## 5. Lo que NO cambia, y por qué

- **Claude Code sigue sin instalarse en el mini.** Es Node, en marcha ocupa cientos de MB
  y los droplets vienen sin swap; en 512 MB el kernel lo mata. Es la única diferencia que
  el usuario acepta y la única que sobrevive a este diseño.
  - Fleco conocido que esto **no** arregla: el mini ofrece los ejecutores `c` y `creset`
    en `/executors` y fallan, porque la federación ata un ejecutor a un **repo** y `c`
    depende de un **binario**. La salida barata sigue siendo un campo
    `requiere: ["claude"]` en el JSON del ejecutor, y vive en el repo del coordinador, no
    aquí.
- **Los dos bots siguen siendo dos.** Ver paso 4: es el `409`, no una preferencia.
- **El tag sigue siendo distinto** (`control` vs `ephemeral`): es lo que hace que
  `apagar-do` se lleve al dev y no al mini. Un tag no es un privilegio, es una etiqueta de
  barrido.
- **Ningún secreto entra en `cloud-init.yaml`.** El llavero viaja después, por SSH y por
  stdin, a ficheros 600. El `user_data` lo sirve la API de metadatos a cualquier usuario
  sin sudo, y eso no cambia con este diseño.
- **A las máquinas de Vast no les llega nada de esto.** No son nuestras: son el ordenador
  de un desconocido alquilado por minutos. `llavero: true` es de `mini` y `dev` y de nadie
  más.

---

## 6. Riesgos, y qué se hace con cada uno

| riesgo | por qué pasa | qué lo contiene |
|---|---|---|
| La clave de flota se filtra y abre **todas** las máquinas | es una sola clave compartida | el techo no sube: quien tiene la máquina ya tiene `DO_TOKEN`. Se rota generando otra, registrándola y pasando `authorize-key` por las vivas |
| Un lanzamiento que hoy funciona a medias **empieza a fallar** | el llavero incompleto pasa a ser fatal | es el objetivo. `--sin-llavero` para el caso de «necesito la máquina ya» |
| El mini se queda **sin disco** al clonar los repos de trabajo | 10 GB, y `foveal-vision-data` crece | medirlo antes (paso 4). Si no cabe, la excepción se escribe aquí |
| Dos bots iguales por error → `409` | copiar un tipo sin cambiar el prefijo | `selected_services()` ya rechaza dos servicios del mismo directorio; el paso 6 lo enseña en `flota` |
| El dev, al ser desechable, lleva ahora **más** secretos | es la paridad que se pide | la exposición real ya existía (`DO_TOKEN`, `GITHUB_TOKEN`). Lo que sí conviene es que destruir un dev sea barato y frecuente, no raro |
| Alguien destruye el mini creyendo que ya da igual | «si son iguales, ¿qué más da?» | da igual en privilegios y **no** da igual en disponibilidad: el mini es el que está siempre encendido. Va en `CLAUDE.md` con el motivo nuevo |

---

## 7. Orden de ejecución

Cada punto es un commit, en el momento, como manda el repo:

1. `llavero.json` + `cargar_llavero()` + `comprobar_llavero()` + `--sin-llavero`
   (paso 1.1-1.3)
2. `build_provision_script()` escribe el llavero + `push-secret --llavero` (paso 1.4-1.5)
3. `clave-flota` + `authorize-key --usuario` + `hacer_lanzador()` reusa la clave (paso 2)
4. `keys --prune` y limpieza de las 26 (paso 2, se puede hacer suelto)
5. `remoto` + `telegram/executors/remoto.json` (paso 3) — **el ejecutor en el mismo commit**
6. `types/mini.json` y `types/dev.json` igualados, tras medir el disco (paso 4)
7. `flota` + su ejecutor (paso 6)
8. Reparación del mini vivo: `clave-flota`, `authorize-key`, `push-secret --llavero`
9. La prueba de aceptación de cuatro pasos (paso 6)
10. Documentación (paso 7)

Los pasos 1-7 se pueden hacer y probar desde la laptop sin tocar el mini vivo. El 8 es el
único que toca producción, y es reparación, no reconstrucción: **el mini no se rehace en
ningún momento de este plan**.
