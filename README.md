# Droplets efímeros en DigitalOcean

Lanza un Droplet bajo demanda, trabaja en él y destrúyelo. Un único script sin
dependencias: sólo Python 3.9+ y `ssh`.

## El droplet que se crea

`s-2vcpu-4gb` — **2 vCPU · 4 GB RAM · 80 GB SSD · $24/mes** ($0.036/hora).

En DigitalOcean el disco **no se elige por separado**: viene fijo con el plan.
Los 80 GB son el mínimo que acompaña a 4 GB de RAM en la gama Basic. La única
opción de 4 GB con disco pequeño (`c-2`, 25 GB) es CPU-Optimized y cuesta
$42/mes, así que pedir menos disco saldría más caro. Detalle en
[docs/digitalocean/droplets-api.md](docs/digitalocean/droplets-api.md).

Ése es el de por defecto, no el único: hay **tipos** con nombre para máquinas
más grandes, con CPU dedicada o **con GPU**, y un comando para ver el catálogo
entero con sus precios. Está en
[Elegir la máquina](#elegir-la-máquina-tipos-catálogo-y-gpu).

## Antes de empezar

Cinco pasos, **todos una sola vez**: valen para todos los droplets que crees
después. Cuentan con que no has hecho nunca nada de esto.

1. [Herramientas en tu máquina](#1-herramientas-en-tu-máquina) — Python, `ssh`, git
2. [Cuenta y token de DigitalOcean](#2-cuenta-y-token-de-digitalocean) — sin esto no se crea ninguna máquina
3. [Token de tu suscripción de Claude](#3-token-de-tu-suscripción-de-claude) — para que Claude Code arranque ya autenticado
4. [Token de GitHub](#4-token-de-github-pat-de-grano-fino) — para clonar tus repos y hacer push desde el droplet
5. [Rellenar el `.env`](#5-el-fichero-env-variable-por-variable) — donde van los tres

Los pasos 3 y 4 son opcionales en sentido estricto: sin ellos el droplet se crea
igual, pero llega vacío de credenciales y tendrás que autenticarte a mano dentro.

### 1. Herramientas en tu máquina

Tres, y en Windows 11 dos suelen venir ya puestas. Nada de esto hace falta
instalarlo *dentro* del droplet: allí lo pone cloud-init solo.

| Necesitas | Para qué | Comprobar con |
|---|---|---|
| **Python 3.9 o superior** | ejecutar el script | `python --version` |
| **Cliente OpenSSH** (`ssh`, `ssh-keygen`) | crear la clave y entrar al droplet | `ssh -V` |
| **git** | clonar este repositorio | `git --version` |

Probado aquí con Python 3.14.6, OpenSSH_for_Windows_9.5p1 y git 2.54.0.

Si te falta alguna:

- **Python** — <https://www.python.org/downloads/>. Marca **"Add python.exe to
  PATH"** en la primera pantalla del instalador; sin eso el comando `python` no
  existirá en la terminal. No hay nada más que instalar: el script usa sólo la
  librería estándar, así que **no hay `pip install` ni entorno virtual**.
- **OpenSSH en Windows** — viene de serie en Windows 10/11. Si `ssh -V` no
  responde: *Configuración → Sistema → Características opcionales → Agregar una
  característica → Cliente de OpenSSH*.
- **git** — <https://git-scm.com/download/win> (en macOS/Linux ya suele estar).

Y clona este repositorio, que es desde donde se ejecuta todo:

```powershell
git clone https://github.com/<usuario>/digital-ocean-dropplet-auto-launching.git
cd digital-ocean-dropplet-auto-launching
```

### 2. Cuenta y token de DigitalOcean

**La cuenta.** Regístrate en <https://cloud.digitalocean.com/registrations/new>.
Te pedirá un medio de pago (tarjeta o PayPal) antes de dejarte crear nada,
aunque tengas crédito de bienvenida. Recuerda que se **factura por segundo
mientras el droplet exista**: el gasto se corta destruyéndolo, no apagándolo.

**El token.** Es la contraseña con la que este script crea y destruye máquinas
en tu cuenta. La ruta en el panel no es evidente:

1. Barra lateral izquierda, **abajo del todo** → **API**
2. Pestaña **Tokens** → botón **Generate New Token**
   (atajo directo: <https://cloud.digitalocean.com/account/api/tokens>)

El formulario:

| Campo | Qué poner |
|---|---|
| **Token name** | Algo que reconozcas dentro de seis meses, p. ej. `droplets-efimeros` |
| **Expiration** | 90 días está bien; *No expiry* si no quieres renovarlo nunca |
| **Scopes** | **Full Access** es lo simple y lo que menos sorpresas da |

Si prefieres afinar, elige *Custom Scopes* y marca lo que este script usa:

| Recurso | Permisos | Para qué |
|---|---|---|
| `droplet` | create, read, delete | `launch`, `list`, `ip`, `destroy` |
| `ssh_key` | create, read | `register-key`, `keys`, y elegir qué claves embeber |
| `actions` | read | esperar a que la creación termine |
| `image`, `sizes`, `regions` | read | los comandos `images`, `sizes` y `regions` |

No hemos probado cada combinación mínima una por una; si un comando te devuelve
**403**, el script imprime la respuesta de la API, que dice qué scope falta.

Pulsa **Generate Token**. El valor **empieza por `dop_v1_`** y DigitalOcean
**sólo te lo enseña una vez**: si cierras la página sin copiarlo, tendrás que
generar otro. Va en `.env` (paso 5) como:

```
DO_TOKEN=dop_v1_...
```

### 3. Token de tu suscripción de Claude

Necesitas dos cosas en **tu máquina** (no en el droplet): una suscripción activa
a Claude (Pro o Max, <https://claude.ai/upgrade>) y Claude Code instalado. Si no
lo tienes:

```powershell
npm install -g @anthropic-ai/claude-code
```

(requiere Node.js, <https://nodejs.org>). Es exactamente lo mismo que el droplet
instala solo por dentro; aquí lo necesitas únicamente para generar el token.

Con eso, en una terminal:

```powershell
claude setup-token
```

Qué pasa: se abre el navegador con la pantalla de autorización de Anthropic,
apruebas, y la terminal imprime un token largo que **empieza por
`sk-ant-oat01-`**. Cópialo y pégalo en `.env`:

```
CLAUDE_CODE_OAUTH_TOKEN=sk-ant-oat01-...
```

Detalles que importan:

- Es un token **de larga duración**, no el de una sesión. Por eso basta con
  generarlo una vez y no hay que repetirlo en cada droplet.
- Consume tu **plan de Claude**, no factura aparte.
- Si algún día deja de funcionar (lo revocaste, caducó), vuelve a ejecutar
  `claude setup-token` y sustituye el valor en `.env`.
- No lo commitees. `.env` está en `.gitignore` justamente por esto.

**Alternativa: `ANTHROPIC_API_KEY`**, sacada de la consola de Anthropic
(<https://console.anthropic.com/settings/keys>). Claude Code la acepta igual,
pero **factura por uso aparte de tu suscripción**. Ponla en `.env` con ese
nombre en lugar de `CLAUDE_CODE_OAUTH_TOKEN` y el resto del flujo es idéntico.

Para comprobar cuál está activa dentro de un droplet:

```bash
claude auth status
```

Responde JSON. `"authMethod":"oauth_token"` = suscripción;
`"authMethod":"api_key"` = facturación por uso; `"loggedIn":false` = no llegó el
token.

### 4. Token de GitHub (PAT de grano fino)

Sirve para que el droplet pueda clonar tus repos privados y hacer push sin que
te pida usuario y contraseña. Necesitas una cuenta de GitHub
(<https://github.com/signup>) y un PAT: una contraseña de un solo propósito, que
tú limitas a los repos y permisos que quieras.

Ruta exacta en la interfaz de GitHub, que no es evidente:

1. Tu foto de perfil (arriba a la derecha) → **Settings**
2. Barra lateral izquierda, hasta abajo → **Developer settings**
3. **Personal access tokens** → **Fine-grained tokens**
4. Botón **Generate new token**

Y el formulario:

| Campo | Qué poner |
|---|---|
| **Token name** | Algo reconocible, p. ej. `droplets-efimeros` |
| **Expiration** | Lo que quieras; recuerda que al caducar habrá que regenerarlo |
| **Resource owner** | Tu usuario (o la organización dueña de los repos) |
| **Repository access** | *Only select repositories* y eliges los que quieras poder tocar, o *All repositories* si prefieres no ir añadiéndolos |
| **Permissions** → *Repository permissions* → **Contents** | **Read and write** |

`Contents: Read and write` es lo único imprescindible: cubre clonar **y** hacer
push. Escribir siempre incluye leer, así que no hace falta marcar nada más para
el flujo normal. Si la interfaz añade sola `Metadata: Read-only`, déjala. Si
además quieres que Claude Code pueda abrir *pull requests* desde el droplet,
añade `Pull requests: Read and write`.

Pulsa **Generate token**, copia el valor (GitHub **sólo lo enseña una vez**) y
complétalo en `.env` junto con tu autoría de commits:

```
GITHUB_TOKEN=<el PAT que acabas de generar>
GIT_USER_NAME=Tu Nombre
GIT_USER_EMAIL=tu@email
DO_REPOS=usuario/proyecto-a,usuario/proyecto-b
```

`GIT_USER_NAME` y `GIT_USER_EMAIL` no son opcionales en la práctica: sin ellos
el primer `git commit` dentro del droplet falla con *"Please tell me who you
are"*.

Para comprobar que el PAT llegó bien, desde dentro del droplet:

```bash
gh auth status        # y, más directo:
gh api user --jq .login
```

Si el token es inválido o le faltan permisos, `provision` te avisa al
inyectarlo y sigue con el resto, así que revisa su salida.

### 5. El fichero `.env`, variable por variable

Toda la configuración vive en un fichero `.env` en la raíz del repositorio. Se
crea copiando la plantilla:

```powershell
Copy-Item .env.example .env      # PowerShell
```

```bash
cp .env.example .env             # macOS / Linux / Git Bash
```

Ábrelo con cualquier editor de texto y rellénalo. El formato es
`CLAVE=valor`, una por línea, **sin comillas y sin espacios alrededor del `=`**.
`.env` está en `.gitignore`: no se commitea nunca, y ahí es donde viven tus tres
tokens.

De todo lo que hay dentro, **sólo `DO_TOKEN` es imprescindible** para crear un
droplet. El resto tiene valores por defecto razonables:

| Variable | ¿Hace falta? | Qué es |
|---|---|---|
| `DO_TOKEN` | **Sí** | El token de DigitalOcean del paso 2 |
| `DO_TYPE` | No — vacío | Tipo de máquina por defecto (plan + imagen + región de una vez). Ver [Elegir la máquina](#elegir-la-máquina-tipos-catálogo-y-gpu) |
| `DO_MAX_PRICE_MONTHLY` | No — `100` | Freno de coste en $/mes: por encima, `launch` pide `--accept-cost`. `0` = sin freno |
| `DO_SIZE` | No — `s-2vcpu-4gb` | Plan de la máquina si no usas un tipo. Míralos con `sizes` |
| `DO_IMAGE` | No — `ubuntu-24-04-x64` | Sistema operativo. Míralos con `images` |
| `DO_REGION` | No — `nyc1` | Centro de datos. Míralos con `regions` |
| `DO_DROPLET_NAME` | No — `proyecto-01` | Nombre del droplet; también lo puedes pasar como argumento a `launch` |
| `DO_TAG` | No — `ephemeral` | Etiqueta para poder limpiarlos todos de golpe |
| `DO_CLOUD_INIT` | No — `cloud-init.yaml` | Plantilla de primer arranque. Ver [La máquina de control](#la-máquina-de-control-lanzar-droplets-desde-el-móvil) |
| `DO_SSH_KEY_FILE` | No — `~/.ssh/do_droplet` | Ruta de tu clave privada. La crea `keygen` |
| `DO_SSH_KEYS` | No — vacío | Vacío = se autorizan **todas** las claves de tu cuenta de DO. Rellénalo sólo para restringir |
| `DO_SSH_USER` | No — `deploy` | Con qué usuario aterrizas al hacer `ssh`. `deploy` es el que tiene tokens y repos |
| `DO_SSH_PORTS` | No — `22,443` | Puertos que se prueban, en orden. El 443 salva las redes que filtran el 22 |
| `DO_DEV_USER` | No — `deploy` | Usuario del droplet que recibe credenciales y repos |
| `CLAUDE_CODE_OAUTH_TOKEN` | Para Claude Code | El token del paso 3 |
| `GITHUB_TOKEN` | Para clonar y hacer push | El PAT del paso 4 |
| `GIT_USER_NAME`, `GIT_USER_EMAIL` | Para commitear dentro | Autoría de tus commits en el droplet |
| `DO_REPOS` | No — vacío | Repos `owner/repo` separados por coma que se clonan solos en `~/src` |
| `DO_SERVICES` | No — vacío | Servicios que quedan corriendo. Ver [Servicios](#servicios-procesos-que-quedan-corriendo) |

Un `.env` mínimo para empezar es literalmente una línea con `DO_TOKEN=`. Uno
completo se parece a esto:

```
DO_TOKEN=dop_v1_...
CLAUDE_CODE_OAUTH_TOKEN=sk-ant-oat01-...
GITHUB_TOKEN=github_pat_...
GIT_USER_NAME=Tu Nombre
GIT_USER_EMAIL=tu@email
DO_REPOS=usuario/proyecto-a,usuario/proyecto-b
```

También puedes definir cualquiera de estas variables como variable de entorno
del sistema: las del entorno tienen prioridad sobre el `.env`. Y para el token
de DigitalOcean se aceptan además los nombres `DIGITALOCEAN_TOKEN` y
`DIGITALOCEAN_ACCESS_TOKEN`, que son los que usan `doctl` y Terraform, por si ya
los tienes puestos.

## Puesta en marcha

Con el `.env` relleno, tres comandos. Se ejecutan **desde la raíz del
repositorio**:

```powershell
python scripts/do_droplet.py keygen        # par de claves ed25519 dedicado
python scripts/do_droplet.py register-key  # lo sube a tu cuenta de DO
python scripts/do_droplet.py launch
```

Qué hace cada uno:

- **`keygen`** crea un par de claves SSH nuevo en `~/.ssh/do_droplet` (privada) y
  `~/.ssh/do_droplet.pub` (pública), dedicado a esto y sin passphrase. Si el
  fichero ya existe no lo toca. La privada **no sale nunca de tu máquina**.
- **`register-key`** sube la pública a tu cuenta de DigitalOcean, que es lo que
  permite que los droplets nuevos te dejen entrar. Si ya estaba subida, lo dice y
  no duplica nada.
- **`launch`** crea el droplet, espera a que la acción termine, espera a que sshd
  acepte conexiones de verdad, espera a que cloud-init acabe de instalar las
  herramientas, inyecta tus tokens y clona tus repos. Al final imprime la IP y el
  comando de conexión. **Tarda unos 5 minutos**; casi todo es Ubuntu
  actualizándose.

Antes de gastar un céntimo puedes ver exactamente qué se enviaría, sin enviarlo:

```powershell
python scripts/do_droplet.py launch --dry-run
```

Y cuando termines, **destruye la máquina** — es lo único que corta la
facturación:

```powershell
python scripts/do_droplet.py destroy
```

## Elegir la máquina: tipos, catálogo y GPU

`launch` sin más crea el droplet de trabajo de siempre. Cuando haga falta otra
cosa —más RAM, CPU dedicada para medir, una GPU— hay un **tipo** con nombre:

```bash
python scripts/do_droplet.py types                  # qué tipos hay y qué cuestan
python scripts/do_droplet.py launch p5 --type big   # lanzar con uno
```

### Ver qué tipos hay

`types` lista los descriptores de [types/](types/) **con el precio traído en
vivo** de la API, para que no haya números viejos en un fichero:

```
dev  ·  s-2vcpu-4gb  ·  $24.00/mes ($0.0357/h)
  El droplet de trabajo de siempre: 2 vCPU compartidas y 4 GB de RAM.
  imagen ubuntu-24-04-x64 · región nyc1 (de .env) · tag ephemeral (de .env)

gpu-h100  ·  gpu-h100x1-80gb  ·  $3,281.04/mes ($4.4100/h)
  NVIDIA H100 con 80 GB de VRAM, 20 vCPU y 240 GB de RAM.
  imagen gpu-h100x1-base · región nyc2 · tag ephemeral (de .env)
  1x nvidia h100 80 GiB · regiones con este plan: ams3, nyc2, tor1
  Ojo: cuesta unas 137 veces el droplet de trabajo...
```

Los que vienen puestos:

| Tipo | Plan | Para qué |
|---|---|---|
| `dev` | `s-2vcpu-4gb` | el de siempre, ~$24/mes |
| `big` | `s-8vcpu-16gb-amd` | compilar, o lo que no cabe en 4 GB (~$112/mes) |
| `cpu` | `c-2` | CPU dedicada: medir tiempos sin ruido de vecinos |
| `mini` | `s-1vcpu-512mb-10gb` | la máquina de control (lleva ya `tag control` y su cloud-init) |
| `gpu-rtx4000` | `gpu-4000adax1-20gb` | la GPU más barata, 20 GB de VRAM (~$565/mes) |
| `gpu-rtx6000` | `gpu-6000adax1-48gb` | 48 GB de VRAM (~$1.168/mes) |
| `gpu-h100` | `gpu-h100x1-80gb` | H100 de 80 GB (~$3.281/mes) |

**Un tipo no es un `size`**: es la combinación entera de plan + imagen + región +
plantilla de arranque + tag. Esa distinción es justo la que hace falta con las
GPU, que además del plan necesitan **su imagen con los drivers puestos**
(`gpu-h100x1-base`); pedir sólo el plan te da una máquina cara con Ubuntu pelado
y sin CUDA.

### Ver el catálogo completo, con precios

`types` son los atajos con nombre. `sizes` es **todo lo que vende
DigitalOcean**, y de ahí sale el `size` de un tipo nuevo:

```bash
python scripts/do_droplet.py sizes                    # de tu región, RAM >= 4 GB
python scripts/do_droplet.py sizes --gpu              # sólo GPU (ver aviso)
python scripts/do_droplet.py sizes --all-regions      # todo, diciendo dónde hay qué
python scripts/do_droplet.py sizes --max-price 50     # hasta $50/mes
python scripts/do_droplet.py sizes --filter c-        # el slug contiene "c-"
python scripts/do_droplet.py sizes --gpu --all        # incluidos los que tu cuenta no tiene
```

```
SLUG                     vCPU      RAM    DISCO      $/MES    $/HORA
gpu-4000adax1-20gb          8    32 GB   500 GB     565.44    0.7600
  1x nvidia rtx4000 ada 20 GiB · tor1
gpu-h100x1-80gb            20   240 GB   720 GB   3,281.04    4.4100
  1x nvidia h100 80 GiB · ams3, nyc2, tor1
```

Se ven las dos unidades a propósito: **la mensual es la que se entiende y la
horaria la que de verdad pagas**, porque estas máquinas viven horas. Un plan que
sólo publica precio por hora se muestra estimado a 730 h, pero eso es raro: hoy
todos publican mensual. **No calcules el mensual multiplicando el horario**:
DigitalOcean usa 672 h en la gama básica y 744 h en las de GPU.

> **Las GPU no están en la mayoría de regiones.** Por eso `--gpu` mira todas
> salvo que pidas una: filtrando por la de tu `.env` (`nyc1`) no aparecía
> ninguna, y la conclusión fácil —"mi cuenta no tiene GPU"— era falsa. La línea
> de detalle dice en qué regiones hay cada plan. Si aun con `--all` no sale
> ninguna, entonces sí: falta pedir acceso a GPU Droplets en el panel.

Y las imágenes de GPU, que **no son distribuciones** y por eso no salían en
`images`:

```bash
python scripts/do_droplet.py images --kind all --filter gpu
```

### Lanzar una GPU

```bash
python scripts/do_droplet.py launch entrena --type gpu-rtx4000 --accept-cost
```

Tres cosas de ese comando:

- **`--region`** porque las GPU no están en todas. Si te equivocas no se crea
  nada: el lanzador comprueba el plan contra `/v2/sizes` antes de gastar un
  céntimo y te dice en qué regiones sí lo hay.
- **`--accept-cost`** es el freno de mano. Por encima de `DO_MAX_PRICE_MONTHLY`
  (100 $/mes por defecto) `launch` se niega y enseña el precio. No es paranoia:
  desde el móvil un tipo mal escrito se manda igual de rápido que el bueno, y
  aquí el error son 3.281 $/mes. Se sube o se quita en el `.env` (`0` = sin
  freno).
- **Cualquier opción suelta pisa al tipo**, así que no hay que editar el
  descriptor para un lanzamiento distinto.

> **Las GPU necesitan cupo aparte, y eso no se puede comprobar antes.** Que el plan
> salga en `sizes --gpu` con su región no basta: la creación puede acabar en
>
> ```
> HTTP 422: creating this/these droplet(s) will exceed your GPU limit
> ```
>
> Comprobado el 2026-08-16 con `gpu-rtx4000` en tor1. No está ni en `/v2/sizes` ni
> en `/v2/account` (que sólo trae `droplet_limit`), así que no hay forma de avisarte
> antes de intentarlo; el lanzador reconoce ese error y te explica qué es. El cupo
> se pide en el panel de DigitalOcean (*Account → Limits*, o por soporte).
>
> Lo tranquilizador: es un **rechazo**, no un droplet a medias. No se crea nada y no
> se factura nada.

Antes de crear nada, el precio sale por pantalla:

```
Creando 'entrena' (tipo gpu-rtx4000): gpu-4000adax1-20gb · gpu-h100x1-base · tor1 · tag ephemeral
Coste: $565.44/mes ($0.7600/h) mientras exista.
```

Y `list` dice lo que estás gastando ahora mismo, que es el número que de verdad
importa cuando se te olvida una máquina encendida:

```
ID          NOMBRE               ESTADO   TAMAÑO                   $/MES  IP
111111111   proyecto-01          active   s-2vcpu-4gb              24.00  164.90.10.20
222222222   entrena              active   gpu-h100x1-80gb       3,281.04  159.65.30.40

Gastando ahora: $4.4457/h  ·  $3,305.04/mes en total.
```

### Añadir un tipo tuyo

Es un fichero, nunca código. `types/loquesea.json`:

```json
{
  "descripcion": "Para qué sirve esta máquina.",
  "size": "s-4vcpu-8gb",
  "image": "ubuntu-24-04-x64",
  "region": "nyc1",
  "cloud_init": "cloud-init.yaml",
  "tag": "ephemeral",
  "notas": "Lo que quieras que se imprima al lanzarlo."
}
```

| Campo | |
|---|---|
| `size` | **Obligatorio.** Slug del plan; sácalo de `sizes` |
| `image`, `region`, `cloud_init`, `tag` | Opcionales: lo que no pongas se hereda del `.env` |
| `descripcion` | Se ve en `types` |
| `notas` | Se imprime **al lanzar**, que es cuando importa avisar |

Nada de esto se valida contra una lista cableada: el plan se comprueba contra
`/v2/sizes` en el momento de lanzar, que es la única fuente de verdad sobre qué
existe, dónde y a qué precio. Los planes **por contrato** no se publican ahí; para
ésos, `--no-check`.

### Elegirlo desde Telegram

Con el ejecutor `lanzar` de la máquina de control **no hay que añadir nada**: ya
pasa lo que escribas a `do_droplet.py`.

```
/use lanzar
types
sizes --gpu
launch entrena --type gpu-rtx4000 --region tor1 --accept-cost
list
destroy entrena --yes
```

Si quieres atajos aún más cortos, se definen con `definer` desde el móvil (son
datos del coordinador, no código de nadie):

```
exec tipos echo timeout=0
cd ~/src/digital-ocean-dropplet-auto-launching && python3 scripts/do_droplet.py types

exec gpu echo timeout=0
cd ~/src/digital-ocean-dropplet-auto-launching && python3 scripts/do_droplet.py launch {{input}} --type gpu-rtx4000 --region tor1 --accept-cost
```

Y a partir de ahí, `/use gpu` y el nombre del droplet como único mensaje.

## Continuar tus proyectos en el droplet

Cada droplet nuevo arranca con **Claude Code instalado, tu sesión iniciada y tus
repos clonados**, sin tocar nada a mano.

### Uso

```bash
python scripts/do_droplet.py launch
python scripts/do_droplet.py ssh
```

Y ya dentro:

```bash
cd ~/src/proyecto-a && claude
```

Para clonar algo que no esté en `DO_REPOS`, sin editar nada:

```bash
python scripts/do_droplet.py launch --repo usuario/otro-proyecto
```

Si el droplet ya existe y sólo quieres reinyectar credenciales o añadir un repo:

```bash
python scripts/do_droplet.py provision --repo usuario/otro-proyecto
```

Dentro del droplet tienes también `gh`, así que `gh repo clone usuario/lo-que-sea`
funciona sin más para cualquier repo que el PAT alcance.

### Dónde acaban los secretos, y por qué no en cloud-init

Los tokens **no viajan en `user_data`**. El motivo es concreto y comprobable: la
API de metadatos del droplet sirve el `user_data` a cualquier usuario, sin sudo.

```bash
# dentro del droplet, como usuario sin privilegios
curl http://169.254.169.254/metadata/v1/user-data     # devuelve el YAML entero
```

Así que `launch` primero espera a que cloud-init termine
(`/var/lib/cloud/DEV_READY`) y **después** empuja los secretos por SSH, por
stdin — no como argumento, que aparecería en el `ps` del droplet. Acaban en
`~deploy/.config/dev-secrets.env` y `~deploy/.git-credentials`, en modo 600.

Como son droplets efímeros, la forma de "revocar" es destruirlos. Si un token se
te escapa, regenéralo en GitHub o con `claude setup-token` otra vez.

## Servicios: procesos que quedan corriendo

Todo lo anterior deja el droplet listo para **trabajar dentro**. Un servicio es
lo otro: un proceso que sigue vivo aunque cierres el SSH, arranca solo si la
máquina se reinicia y se levanta otra vez si se cae.

Cada servicio es un fichero en [services/](services/). El lanzador no sabe nada
de ningún proyecto en concreto: sabe clonar un repo, instalarlo y registrarlo
como unidad de systemd. Lo que cambia va en el descriptor:

```json
{
  "repo": "usuario/mi-servicio",
  "install": "npm ci",
  "start": "npm start",
  "env_prefix": "MI_"
}
```

| Campo | |
|---|---|
| `repo` | **Obligatorio.** `owner/repo`. Se clona solo en `~/src/<repo>`; no hace falta repetirlo en `DO_REPOS` |
| `start` | **Obligatorio.** Comando de arranque, ejecutado dentro del repo |
| `install` | Se ejecuta una vez antes de arrancar (`npm ci`, `pip install -r ...`) |
| `env_prefix` | Puente de configuración: cada `MI_ALGO=x` del `.env` del lanzador se escribe como `ALGO=x` en el `.env` del servicio, dentro del droplet, en modo 600 |
| `env_file` | Nombre de ese fichero. Por defecto `.env` |

Se activan por nombre, en `.env` o en la línea de comandos:

```bash
DO_SERVICES=telegram-coordinator          # en .env, para todos los lanzamientos
python scripts/do_droplet.py launch --service telegram-coordinator
```

Y se manejan sin recordar la sintaxis de systemd:

```bash
python scripts/do_droplet.py service status  telegram-coordinator
python scripts/do_droplet.py service logs    telegram-coordinator --lines 100
python scripts/do_droplet.py service follow  telegram-coordinator   # en vivo
python scripts/do_droplet.py service restart telegram-coordinator
```

`env_prefix` existe porque la configuración de un servicio suele ser secreta y
**no puede vivir en su repo ni en `cloud-init.yaml`**: el `user_data` lo lee
cualquier usuario del droplet sin sudo. Viaja por el mismo canal que los tokens,
por SSH y por stdin.

### El servicio incluido: `telegram-coordinator`

Un bot de Telegram que enruta tus mensajes al shell del droplet y a Claude Code,
con una conversación independiente por cada tema del grupo. Es lo que convierte
el droplet en algo que puedes seguir usando desde el móvil.

Necesita dos cosas en el `.env` **de este** repositorio:

```
DO_SERVICES=telegram-coordinator
TG_BOT_TOKEN=123456:ABC...        # te lo da @BotFather con /newbot
TG_ALLOWED_USER_IDS=99887766      # tu id; escríbele /whoami al bot para saberlo
TG_CLAUDE_PERMISSION_MODE=bypassPermissions
```

Con eso, `launch` deja el bot respondiendo. Tres cosas que conviene saber antes:

- **`ALLOWED_USER_IDS` es la única protección.** El bot ejecuta comandos en el
  droplet por diseño: quien esté en esa lista tiene shell. Con el id vacío no
  atiende a nadie (es lo seguro); con el id equivocado atiende a otro.
- **Sólo puede haber una instancia haciendo polling.** Si tienes el coordinador
  corriendo en la laptop, párala antes o Telegram devolverá error 409 a una de
  las dos.
- **No hace falta abrir ningún puerto**: el bot usa *long polling*, sale él hacia
  Telegram. `ufw` se queda como está, sólo SSH.

Para llevar un cambio tuyo del coordinador al droplet, sin salir de Telegram:

```
cd ~/src/telegram-coordinator && git pull && sudo systemctl restart telegram-coordinator
```

Y ojo con esto al destruir: **los ejecutores que crees desde el móvil viven en el
droplet**. Son ficheros JSON en `data/`, versionables — si quieres conservarlos,
haz `git push` desde el droplet antes de destruirlo. Ten en cuenta también que un
droplet con un servicio dentro es un droplet de larga vida: no lo barras con
`destroy --tag ephemeral` sin mirar, y recuerda que factura mientras exista.

## La máquina de control: lanzar droplets desde el móvil

> ### El mini no se destruye nunca en una limpieza
>
> **`mini` está excluido de cualquier borrado masivo.** "Borra todos los
> droplets", "limpia lo que haya quedado" o `destroy --tag ephemeral` **no le
> afectan**: significan *todas las máquinas de trabajo*, nunca la de control.
>
> Sólo se destruye cuando lo pidas **por su nombre y a propósito**
> (`destroy mini`). Si estás automatizando una limpieza, exclúyelo; y ante la
> duda, pregunta antes de tocarlo.
>
> El motivo es práctico: es la máquina desde la que lanzas todo lo demás cuando
> no tienes la laptop delante. Perderla estando fuera de casa te deja sin
> ninguna forma de crear ni destruir droplets desde el móvil, y rehacerla exige
> volver a la laptop. Cuesta $4/mes; borrarla por error sale mucho más caro que
> mantenerla.

La idea: una máquina pequeña y **siempre encendida** cuyo único trabajo es crear
y destruir las grandes. Le escribes por Telegram desde el móvil, ella lanza un
droplet de trabajo, y cuando terminas lo destruye. Cuesta **$4/mes**
(`s-1vcpu-512mb-10gb`) frente a los $24/mes de una de trabajo, que ya sólo pagas
mientras la usas.

### Por qué necesita su propio arranque

Con 512 MB **no cabe Claude Code**. Es una aplicación de Node que en marcha ocupa
cientos de MB, y los droplets vienen **sin swap**: no es que vaya lento, es que
el kernel mata el proceso. Por eso hay dos plantillas:

| | |
|---|---|
| `cloud-init.yaml` | Droplets de trabajo: Node, Claude Code, `gh`, Python, `uv` |
| `cloud-init.mini.yaml` | Control: Node (para el bot), `git`, `gh`, **1 GB de swap** y nada más |

Se elige con `--cloud-init` o con `DO_CLOUD_INIT` en el `.env`.

### Y su propio bot

**Tiene que ser un bot distinto del que corre en los droplets de trabajo.**
Telegram sólo admite **un** proceso haciendo long polling por token: el segundo
recibe un `409` y se queda fuera. Créalo en [@BotFather](https://t.me/BotFather)
con `/newbot` y ponle un nombre que distingas — *Lanzador* frente a
*Coordinador*. En el móvil son dos chats separados, y la división es la natural:
**al Lanzador le pides máquinas, al Coordinador le pides trabajo**.

En el `.env`:

```
TGL_BOT_TOKEN=<el del bot Lanzador>
TGL_ALLOWED_USER_IDS=<tu id de Telegram>
TGL_DO_TOKEN=<tu token de DigitalOcean>
```

`TGL_DO_TOKEN` es lo que convierte al mini en lanzador. Llega al `.env` del bot y
de ahí al entorno de los comandos que ejecuta, así que `do_droplet.py` lo
encuentra sin configuración extra.

> **Quien pueda hablarle a ese bot puede crear y destruir máquinas en tu cuenta**,
> es decir, gastar tu dinero. `TGL_ALLOWED_USER_IDS` es la única barrera. No la
> dejes vacía y no metas a nadie que no seas tú.

### Crearla

```powershell
python scripts/do_droplet.py launch mini `
  --size s-1vcpu-512mb-10gb `
  --cloud-init cloud-init.mini.yaml `
  --tag control `
  --service telegram-launcher `
  --repo stalinbeltran/digital-ocean-dropplet-auto-launching `
  --push-do-token `
  --push-env DO_SIZE,DO_IMAGE,DO_REGION,DO_TAG,DO_SSH_USER,DO_SSH_PORTS,DO_DEV_USER `
  --push-env DO_REPOS,DO_SERVICES,GIT_USER_NAME,GIT_USER_EMAIL `
  --push-env TG_BOT_TOKEN,TG_ALLOWED_USER_IDS,TG_CLAUDE_PERMISSION_MODE
```

`--push-env` copia al mini las variables que le nombres. **Sin ellas lanza
droplets peores que los tuyos y no te enteras hasta que entras en uno**: su copia
del lanzador no tiene `.env` (está gitignorado), así que sin esto crearía máquinas
sin tus repos, sin autoría de git y sin el bot Coordinador. Se puede repetir y
admite comas; una variable que aquí esté vacía se avisa y no se envía.

`--repo` limita el clonado a este repo. Si lo omites, el mini se clonaría también
lo que tengas en `DO_REPOS`, que en una máquina de 512 MB no pinta nada.

`--push-do-token` es lo que la convierte en lanzador: envía tu token de
DigitalOcean a `~deploy/.config/dev-secrets.env`, junto a los demás secretos.
**Es una opción de línea de comandos y no una variable del `.env` a propósito**,
para que no se te cuele en todos los droplets sin darte cuenta. Úsala sólo aquí.

Sin ella el bot funcionaría igual — lee el token de su propio `.env` — pero
cualquier comando que lances entrando por SSH a la máquina fallaría con un
"falta el token", incluido el `register-key` de aquí abajo.

El `--tag control` no es decorativo: si llevara el tag `ephemeral`, un
`destroy --tag ephemeral --yes` se llevaría por delante tu lanzador.

### Dos pasos que quedan dentro de la máquina

**1. Su propia clave SSH.** Sin esto crea droplets en los que luego no puede
entrar a aprovisionar. Es el mismo flujo multi-máquina de más abajo, pero
ejecutado en el mini:

```bash
python scripts/do_droplet.py ssh mini --cmd "sudo -u deploy -H bash -lc '
  cd ~/src/digital-ocean-dropplet-auto-launching &&
  python3 scripts/do_droplet.py keygen &&
  python3 scripts/do_droplet.py register-key --name mini'"
```

**2. Un ejecutor sin límite de tiempo.** El coordinador corta los comandos a los
30 s y un `launch` tarda unos 5 minutos, así que con el ejecutor `shell` normal
el lanzamiento moriría a medias. Desde Telegram, con `/use definer`:

```
exec lanzar echo timeout=0
cd ~/src/digital-ocean-dropplet-auto-launching && python3 scripts/do_droplet.py {{input}}
```

Y a partir de ahí, desde el móvil:

```
/use lanzar
launch proyecto-05
list
destroy proyecto-05 --yes
```

### Actualizar su configuración sin recrearla

Cuando cambies el `.env` de tu laptop, **el mini no se entera**: su configuración
es la copia que le dejaste en `~deploy/.config/dev-secrets.env` la última vez.
Los droplets que cree seguirán saliendo con los valores viejos.

No hay que destruirla ni volver a lanzarla. Se re-aprovisiona, que reescribe esa
copia y reinicia el bot:

```powershell
python scripts/do_droplet.py provision mini `
  --service telegram-launcher `
  --repo stalinbeltran/digital-ocean-dropplet-auto-launching `
  --push-do-token `
  --push-env DO_SIZE,DO_IMAGE,DO_REGION,DO_TAG,DO_SSH_USER,DO_SSH_PORTS,DO_DEV_USER `
  --push-env DO_REPOS,DO_SERVICES,GIT_USER_NAME,GIT_USER_EMAIL `
  --push-env TG_BOT_TOKEN,TG_ALLOWED_USER_IDS,TG_CLAUDE_PERMISSION_MODE
```

Es el mismo bloque de opciones que el `launch`, sin las que sólo valen al crear
la máquina (`--size`, `--cloud-init`, `--tag`). Tarda menos de un minuto: los
repos ya están clonados y sólo rehace credenciales, el `.env` del bot y la unidad
de systemd.

#### Para cambiar un solo parámetro, el comando es el mismo

**No existe un "empujar sólo esta variable", y no es un olvido:**
`dev-secrets.env` se escribe entero de una vez (`cat >`), no se va añadiendo. Lo
que no venga en el comando **desaparece del mini**. Si quieres cambiar sólo
`DO_SIZE`, el procedimiento es:

1. editas `DO_SIZE` en tu `.env`,
2. ejecutas **el comando completo de arriba**, tal cual, sin quitar nada.

Quitar `--push-do-token` porque "el token no ha cambiado" deja al mini sin
`DO_TOKEN`, y acortar la lista de `--push-env` borra las que falten. El fichero
final es exactamente lo que diga ese comando.

Dos consecuencias más de lo mismo:

- **Un `TGL_*` cambiado necesita `--service telegram-launcher`** en el comando:
  es lo que reescribe el `.env` del bot y hace `systemctl restart`. Sin esa
  opción el bot sigue con el token o la allowlist anteriores.
- **`provision` no hace `git pull`.** Si el cambio está en el código y no en el
  `.env`, esto no lo trae: para eso está
  [actualizar el código](#actualizar-el-código-desde-el-móvil), aquí debajo.

Y los **droplets de trabajo que el mini ya había creado no cambian**: la
configuración se aplica en el momento del `launch`. A los que sigan vivos,
`provision <nombre>` uno a uno.

Para comprobar qué quedó dentro sin sacar ningún secreto a la pantalla:

```powershell
python scripts/do_droplet.py ssh mini --cmd "sed -n 's/^export \([A-Z_]*\)=.*/  \1/p' ~/.config/dev-secrets.env"
```

El `--yes` de `destroy` **no es opcional aquí**: el coordinador le cierra el stdin
al comando, así que no hay teclado donde escribir la confirmación. Sin él, el
comando se niega a destruir nada y te lo dice.

### Actualizar el código desde el móvil

Lo anterior mueve **configuración**. Esto mueve **código**: lo que corriges en la
laptop y subes a GitHub no llega solo al mini, y el bot además tiene ya cargado
en memoria el que había al arrancar, así que aunque el repo se actualice **sigue
ejecutando el viejo sin que nada lo delate**.

Desde Telegram, con el ejecutor `actualizar`:

```
/use actualizar
ya
```

Sirve cualquier texto: el ejecutor ignora lo que escribas. Contesta algo así:

```
Actualizando mini (/home/deploy/src):
  digital-ocean-dropplet-auto-launching: ya estaba al día (76b327e)
  telegram-coordinator: 185a567 -> d78a11b (3 commits)
Servicios:
  telegram-launcher: se reinicia en 3 s (es quien está ejecutando esto)
```

Qué hace, en este orden:

1. `git pull --ff-only` en **cada** repo de `~/src`. Con `--ff-only` a propósito:
   si la copia del droplet tiene commits propios, lo que hace falta es enterarse,
   no fabricar un merge a ciegas desde un bot.
2. `npm ci` **sólo** si el pull tocó `package.json` o el lock. Uno de más son
   minutos en una máquina de 512 MB, con el servicio parado mientras tanto.
3. `systemctl restart` de los servicios cuyo repo ha cambiado, y sólo de ésos.

**El bot se reinicia a sí mismo, y por eso el mensaje llega igual.** Cuando el
update lo pides tú por Telegram, el proceso que lo ejecuta es hijo del bot: al
parar su unidad, systemd mata el cgroup entero, ese proceso incluido, con la
respuesta todavía sin enviar. Su reinicio se programa con
`systemd-run --on-active=3`, que vive fuera del cgroup, así que el bot tiene esos
segundos para contestarte antes de irse. Vuelve solo en un par de segundos.

Lo mismo desde la laptop, sin Telegram de por medio, o en cualquier droplet de
trabajo (`ssh <nombre>` en vez de `ssh mini`):

```powershell
python scripts/do_droplet.py ssh mini --cmd "cd ~/src/digital-ocean-dropplet-auto-launching && python3 scripts/do_droplet.py update"
```

`update` es el único subcomando que actúa sobre la máquina donde se ejecuta en
vez de sobre la API de DigitalOcean: **corre dentro del droplet**. Lanzarlo en la
laptop no hace nada, se niega y te recuerda la forma de arriba.

Si ya hiciste el pull a mano y sólo quieres que el servicio recoja el código
(o si un reinicio anterior falló), `update --restart-all` reinicia aunque no haya
cambios. Desde Telegram va con el ejecutor `lanzar`, que sí admite argumentos:

```
/use lanzar
update --restart-all
```

## Dar a un droplet el token de DigitalOcean (y acceso al mini)

A veces un droplet de trabajo necesita hablar con la API: probar `sizes`, mirar
qué hay vivo, o lanzar él mismo otra máquina. Hay un comando para eso, y **no es
`provision`**:

```bash
python scripts/do_droplet.py push-do-token foveal
```

### Por qué no `provision --push-do-token`

Porque `provision` **reescribe `dev-secrets.env` entero** (`cat >`), y eso es
deliberado: el fichero acaba siendo exactamente lo que diga el comando. Usarlo
sólo para añadir el token **borra del destino todo lo que el emisor no tenga a
mano** —el de Claude, el de GitHub—, y no se nota al momento: se nota cuando
algo dentro de esa máquina deja de autenticar sin motivo aparente.

`push-do-token` toca **una línea** y deja el resto como estaba. Repetirlo **rota**
el token: quita la línea anterior y pone la nueva.

Del resto se encarga igual que `provision`: el token viaja por SSH, dentro del
script que va por **stdin** (nunca como argumento de `ssh`, que saldría en el
`ps` del destino), y acaba en `~deploy/.config/dev-secrets.env` en modo 600, con
la línea que lo carga en cada shell.

> **Quien entre a esa máquina podrá crear y destruir droplets en tu cuenta**, es
> decir gastar dinero. Dáselo sólo a máquinas tuyas y destrúyelas al acabar.
>
> Si lo que quieres es que sólo pueda *mirar*, crea en el panel un token con
> *Custom Scopes* de sólo lectura (`droplet:read`, `image:read`, `sizes:read`,
> `regions:read`, `ssh_key:read`), guárdalo aparte y mándalo con:
>
> ```bash
> python scripts/do_droplet.py push-do-token foveal --from-env DO_TOKEN_RO
> ```
>
> Llega al destino como `DO_TOKEN`, así que todo funciona igual salvo crear y
> destruir. Para consultar el catálogo, comprobar planes o hacer `--dry-run`
> basta con eso.

Desde Telegram, con el ejecutor `lanzar` de la máquina de control:

```
/use lanzar
push-do-token foveal
```

### Entrar por SSH a la máquina de control

La otra mitad: que un droplet pueda **entrar** al mini (o a cualquier otra
máquina). La clave privada no viaja nunca; sólo se autoriza la pública allí
donde se quiere entrar.

En el droplet que quiere entrar:

```bash
python scripts/do_droplet.py keygen --comment mi-droplet   # si no la tiene ya
cat ~/.ssh/do_droplet.pub
```

Y en la máquina de destino —desde Telegram, que es donde ya tienes un comando—:

```
/use lanzar
authorize-key ssh-ed25519 AAAAC3Nza... mi-droplet
```

`authorize-key` corre **dentro** de la máquina donde se quiere entrar, no contra
la API. Es idempotente (compara el material de la clave, no el comentario), deja
`authorized_keys` en modo 600 y se niega si lo que le pasas no es una clave.

Luego, ya con el token puesto, el droplet encuentra solo la IP del mini:

```bash
python scripts/do_droplet.py ssh mini --cmd "uptime"
```

> **Esto es un permiso mayor que el token.** Shell en el mini es el token *más*
> la capacidad de destruirlo todo, y el mini es la máquina desde la que lanzas
> cuando no tienes la laptop delante. Dáselo sólo a droplets tuyos y recuerda
> que [el mini no se destruye nunca en una limpieza](#la-máquina-de-control-lanzar-droplets-desde-el-móvil).

## Volúmenes: lo único que sobrevive al droplet

Un droplet se rehace sin aviso y su disco se va con él. Un **volumen de bloques**
no: existe aparte, se conecta a la máquina que lo necesite y sigue ahí cuando esa
máquina ya no está. Por eso es donde va lo que cuesta caro reconstruir —en este
montaje, el dataset del benchmark de vCPU: mil imágenes renderizadas con Chromium,
reproducibles pero lentas de generar (ver `docs/benchmark-vcpu.md` en
`foveal-vision`).

```bash
python scripts/do_droplet.py volume create bench-data --size-gb 10
python scripts/do_droplet.py volume list
python scripts/do_droplet.py launch trabajo --volume bench-data   # conecta y monta
```

Queda montado en `/mnt/<nombre>` y con una entrada en `/etc/fstab`, así que
sobrevive a los reinicios de la máquina. `attach` formatea **sólo** si el disco
viene en blanco: lo decide `blkid`, no una suposición, porque formatear un
volumen con dato dentro es exactamente lo que no se puede permitir.

Tres cosas que conviene saber antes de diseñar nada encima:

- **Un volumen se conecta a UN droplet a la vez.** No es un disco compartido. Para
  repartir su contenido entre varias máquinas se copia por SSH desde la que lo
  tiene. Para un benchmark eso además es lo correcto: hay que medir leyendo de
  disco local, no de un disco de red.
- **No se mueve de región.** El volumen y el droplet tienen que estar en la misma;
  `launch --volume` lo comprueba **antes** de crear nada, para que el error salga
  gratis y no te deje una máquina facturando sin el disco que ibas a usar.
- **Se paga por existir**, 0,10 $/GB al mes, esté conectado o no. 10 GB son 1 $/mes:
  esa es la cuota por no tener que regenerar el dataset nunca más.

`volume detach` desmonta antes de desconectar (al revés se pierde lo que el kernel
tenga sin escribir) y `volume destroy` pide confirmación y se niega si sigue
conectado.

## Una máquina que lanza otras máquinas

El token deja a un droplet **crear** droplets, pero no **entrar** en ellos, y eso
se descubre tarde: las máquinas se crean, facturan, y su creador no puede
conectarse. La razón es que un droplet acepta las claves públicas registradas en
la cuenta **en el momento de crearlo**, así que la lanzadora necesita un par
propio, registrado antes de lanzar nada.

`--make-launcher` hace las tres cosas de una vez, que es el punto: por separado se
olvida una.

```bash
python scripts/do_droplet.py launch trabajo \
  --service telegram-coordinator \
  --make-launcher \
  --volume bench-data \
  --repo stalinbeltran/foveal-vision \
  --repo stalinbeltran/image-text-sample-generator
```

- envía el `DO_TOKEN` (implica `--push-do-token`),
- clona el repo del lanzador en `~/src`,
- genera un par de claves **dentro** del droplet y registra la pública en la
  cuenta como `lanzador-<nombre>`. La privada no viaja: se queda donde nació.

> ⚠️ Quien entre en esa máquina puede crear y destruir droplets en tu cuenta. Con
> el servicio `telegram-coordinator` encima, eso incluye a quien hable con el bot:
> la allowlist de Telegram es la única barrera.

Las claves `lanzador-*` se acumulan en la cuenta según se rehacen máquinas.
Míralas con `keys` y borra las de máquinas que ya no existen.

## Acceso desde tu otra laptop

Están contempladas las dos formas. **La segunda es la recomendada.**

### Opción A — llevarte la misma clave

Rápida, pero la clave privada viaja. Cópiala por un canal seguro (memoria USB,
gestor de contraseñas; **nunca** por email o chat) y arregla los permisos:

```bash
# en la otra laptop
mkdir -p ~/.ssh && cp /ruta/do_droplet ~/.ssh/
chmod 600 ~/.ssh/do_droplet          # Linux / macOS
```

```powershell
# en Windows, si SSH se queja de permisos "too open"
icacls "$env:USERPROFILE\.ssh\do_droplet" /inheritance:r /grant:r "$env:USERNAME:R"
```

### Opción B — una clave propia por máquina (recomendada)

Cada laptop genera su clave y la registra. La privada nunca se mueve de su
máquina, y si pierdes una puedes revocarla sola sin afectar a la otra.

```bash
# en la otra laptop: clona el repo, crea su .env con el mismo token, y luego
python scripts/do_droplet.py keygen
python scripts/do_droplet.py register-key --name laptop-2
```

Con `DO_SSH_KEYS` vacío en `.env` (el valor por defecto), **cada droplet nuevo
embebe todas las claves registradas en la cuenta**, así que a partir de ese
momento ambas máquinas entran sin más.

Si el droplet **ya estaba corriendo** cuando registraste la segunda clave, no la
tiene: la lista de claves se fija en el arranque. Añádela en caliente desde la
máquina que sí tiene acceso:

```bash
python scripts/do_droplet.py ssh --cmd "echo 'ssh-ed25519 AAAA... laptop-2' >> ~/.ssh/authorized_keys"
```

## Si tu red bloquea SSH

El droplet escucha SSH en el **22 y en el 443** (cloud-init configura
`ssh.socket`; en Ubuntu 24.04 sshd va activado por socket y la directiva `Port`
de `sshd_config` se ignora). `launch` prueba ambos y usa el primero que
responda.

Aun así hay redes corporativas donde **ninguno** sirve: un appliance de
inspección TLS acepta el TCP del 443 de cualquier destino y luego corta todo lo
que no sea TLS, con lo que SSH muere en el intercambio de claves
(`kex_exchange_identification: Connection reset by peer`).

Para saber si estás en ese caso:

```bash
python -c "import socket;s=socket.create_connection(('198.51.100.77',443),timeout=8);print('hay proxy transparente')"
```

Si eso "conecta" con una IP que no existe, tu red intercepta el 443 y el SSH
directo no va a funcionar desde ahí. Alternativas:

- **Consola web de DigitalOcean** — va por HTTPS, atraviesa el proxy:
  `https://cloud.digitalocean.com/droplets/<id>/console`
- Conectarte desde una red sin filtrar (casa, móvil compartido).

Por eso `launch` no se fía de que el TCP conecte: espera el banner `SSH-2.0-…`
antes de dar el puerto por bueno.

## Uso diario

```bash
python scripts/do_droplet.py launch            # crear, esperar y aprovisionar
python scripts/do_droplet.py launch p5 --type big     # con otro tipo de máquina
python scripts/do_droplet.py launch --no-provision   # crudo, sin credenciales
python scripts/do_droplet.py provision         # reinyectar credenciales y repos
python scripts/do_droplet.py list              # qué hay vivo
python scripts/do_droplet.py ssh               # conectar
python scripts/do_droplet.py ip                # sólo la IP
python scripts/do_droplet.py service logs telegram-coordinator  # log de un servicio
python scripts/do_droplet.py update            # DENTRO del droplet: git pull + reinicios
python scripts/do_droplet.py destroy           # destruir (pide confirmación)
python scripts/do_droplet.py destroy --tag ephemeral --yes   # limpieza de las de trabajo
```

La "limpieza total" es total **para las máquinas de trabajo**. El mini de
control no lleva ese tag y no debe borrarse en una limpieza: sólo por su nombre.

Antes de gastar nada, comprueba qué se va a enviar:

```bash
python scripts/do_droplet.py launch --dry-run
```

Descubrir slugs vigentes en lugar de fiarte de los del `.env`:

```bash
python scripts/do_droplet.py types                        # tipos con nombre y su precio
python scripts/do_droplet.py sizes --region nyc1          # catálogo de planes
python scripts/do_droplet.py sizes --gpu                  # los de GPU, en todas las regiones
python scripts/do_droplet.py regions
python scripts/do_droplet.py images --filter ubuntu
python scripts/do_droplet.py images --kind all --filter gpu   # las imágenes con drivers
```

## Vast.ai: comprobar el token antes de gastar

Además de DigitalOcean, este repo empieza a mirar **Vast.ai** para el trabajo de
comparar velocidades de GPU. El porqué de ese proveedor y no otro está en
[gpu_training_services.md](gpu_training_services.md); aquí sólo va el trámite de
que la clave entre.

Pon la clave en `.env` (se crea en <https://cloud.vast.ai/manage-keys/>):

```
VAST_AI_API_TOKEN=tu_clave_de_vast
```

Y compruébala:

```bash
python scripts/vast_check.py
```

Salida real de una cuenta recién creada:

```
Comprobando el token de Vast.ai (sin alquilar nada)

  [OK   ] identidad            autenticado como tu@correo.com (id 618822)
  [AVISO] saldo                saldo 0,00 $: el token lee bien, pero no podrás alquilar hasta cargar credito en https://cloud.vast.ai/billing/
  [OK   ] catalogo             64 ofertas, 16 modelos de GPU; la más barata es Tesla V100 a 0.027 $/GPU-h
  [OK   ] benchmarks           200 marcas sobre 21 modelos de GPU
  [OK   ] instancias           ninguna instancia encendida (no se está facturando nada)
  [AVISO] claves-ssh           no hay ninguna clave SSH registrada; hará falta una antes de alquilar (POST /api/v0/ssh/ o https://cloud.vast.ai/manage-keys/)
  [OK   ] catalogo-sin-clave   responde sin token (4 ofertas)

  El token funciona. 2 aviso(s): lee arriba antes de lanzar.
```

Las cifras del catálogo bailan de una ejecución a otra: es un marketplace vivo y
la oferta cambia por minutos. Lo que importa es el `[OK]`, no el número.

**No alquila nada ni gasta un céntimo.** Todas las llamadas son de lectura salvo
la del catálogo, que es un `POST` porque así busca la API de Vast.ai; lo único
que alquila una máquina es un `PUT` a `/asks/`, y ese no se hace aquí.

Qué mira cada prueba:

| prueba | para qué |
|---|---|
| `identidad` | que la clave entra: `GET /api/v0/users/current/` |
| `saldo` | crédito de la cuenta; **aviso**, no fallo, porque una clave válida en una cuenta sin fondos autentica bien y sólo falla al alquilar |
| `catalogo` | `POST /api/v0/bundles/`, la búsqueda del marketplace, y que traiga `dlperf` |
| `benchmarks` | `GET /api/v0/benchmarks/`, las puntuaciones ya medidas por Vast.ai |
| `instancias` | qué hay encendido **ahora mismo** y cuánto cuesta la hora |
| `claves-ssh` | claves registradas; hace falta una antes de alquilar |
| `catalogo-sin-clave` | que el catálogo siga respondiendo sin token |

Sale con **0** si todo lo imprescindible pasa y con **1** si algo falla, así que
sirve tal cual en un script. Los avisos no hacen fallar: el token es correcto, lo
que falta es dinero o una clave SSH.

Con `--json` la salida es JSON, para encadenarla con otra cosa:

```bash
python scripts/vast_check.py --json
```

Dos cosas útiles cuando algo va mal:

- **Si falla todo menos `catalogo-sin-clave`, el problema es el token, no la
  red.** Esa prueba llama al mismo servidor sin autenticar: si ella pasa y las
  demás dan 401, la conexión está bien y lo que está mal es la clave.
- **Vast.ai permite claves con permisos recortados.** Una de sólo lectura pasa
  `identidad` y `catalogo` pero no podrá alquilar, y eso no se ve hasta el
  momento de crear la instancia. Si la vas a usar para lanzar, créala con
  permiso de escritura sobre instancias.

## Datasets: que el dato llegue a cualquier máquina

El problema es viejo y siempre igual: **los datos no están en el repo del
proyecto**, porque son miles de binarios y van gitignoreados. Una máquina recién
creada clona el código y se queda sin dato, y eso no se descubre hasta que el
trabajo ya está pagado y fallando. Con Vast.ai además no vale la solución de
DigitalOcean: **un volumen de bloques no se conecta a una máquina de otro
proveedor**.

[datasets/](datasets/) es un JSON por dataset — dato y no código, igual que
`types/` y `services/`. Añadir uno es escribir un fichero.

```bash
python scripts/dataset.py list                    # qué hay declarado
python scripts/dataset.py fetch dirty-1000-80px   # traerlo y dejarlo listo
python scripts/dataset.py check                   # ¿cuadra con lo declarado?
python scripts/dataset.py pack dirty-1000-80px    # fabricar el tar.gz + sha256
```

### Tres formas de que viaje, y cuándo usar cada una

Cada dataset declara **varias fuentes** y se prueban en orden. Ninguna sirve para
todos los tamaños:

| fuente | cómo llega | úsala cuando | el precio |
|---|---|---|---|
| **`repo`** | un `tar.gz` commiteado aquí; llega con `git clone` | hasta unas **decenas de MB**, y el dato está congelado | engorda el repo para siempre |
| **`url`** | descarga pública (release de GitHub, S3…) | **cientos de MB o GB**; la máquina se lo baja sola, sin pasar por tu conexión | hay que publicarlo y mantenerlo vivo |
| **`local`** | copia de la máquina que lo tenga | aún no has publicado nada, o es privado | sólo funciona desde esa máquina |

`dirty-1000-80px` (2.002 ficheros, **8,6 MB** comprimidos) va por `repo`: el
benchmark queda listo con sólo clonar, en DigitalOcean, en Vast.ai o donde sea.

Para el siguiente dataset, la regla práctica: **si pasa de ~50 MB, publícalo y
usa `url`.** El flujo es el mismo:

```bash
python scripts/dataset.py pack mi-dataset          # da el sha256
gh release create datasets-v1 datasets/blobs/mi-dataset.tar.gz \
  --repo stalinbeltran/foveal-vision --title "Datasets" --notes "..."
# y en datasets/mi-dataset.json, cambia la fuente `repo` por `url`
```

### Por qué todo lleva sha256

Un dataset a medias o cambiado **da números con el mismo aspecto y otro
significado**, que es peor que no dar ninguno. Ya pasó una vez en este proyecto:
`bench-synth-16` y `bench-dirty1000-16` no se comparan entre sí, y el reporte
guarda el nombre del dataset precisamente por eso.

El empaquetado es **determinista** — orden fijo, `uid`/`gid`/`mtime` a cero — así
que dos `pack` del mismo dato dan el mismo checksum (comprobado). Sin eso el
sha256 significaría "lo hizo la misma máquina el mismo día" en vez de "es el
mismo dato".

Un benchmark declara los suyos por nombre y `vast_instance.py` los resuelve,
verifica y coloca en su sitio **antes de alquilar nada**: si falta el dato, el
barrido muere gratis en vez de con la máquina encendida.

```json
"datasets": ["dirty-1000-80px"]
```

## Medir velocidad: un barrido de máquinas en Vast.ai

El objetivo es responder a *cuánto acelera mi entrenamiento si le doy más CPU*,
con números medidos y no con intuiciones. El montaje son dos piezas y cada una
está donde tiene sentido:

- **La máquina de control es un droplet de DigitalOcean.** Ahí corre Claude, ahí
  vive el dataset y desde ahí se dispara el barrido. Es de larga vida.
- **Las máquinas de medir se alquilan en Vast.ai**, viven los minutos que dura la
  medida y se destruyen solas. Salen entre 0,05 y 0,10 $/h, así que un barrido de
  cinco niveles cuesta **céntimos**.

### Paso 1 — el droplet de control

```powershell
python scripts/do_droplet.py launch bench-control `
  --make-launcher `
  --push-env VAST_AI_API_TOKEN `
  --repo stalinbeltran/foveal-vision
```

`--push-env VAST_AI_API_TOKEN` es lo que hace que el droplet pueda alquilar en
Vast; sin él, `vast_instance.py` allí dentro dirá que falta el token. Los repos
quedan en `~/src`.

El dataset **no hay que subirlo**: viaja en este repo y lo resuelve el registro
de [datasets/](datasets/). Ése era el paso manual que fallaba siempre.

### Paso 2 — dentro del droplet, darle una clave en Vast

**El token deja alquilar, pero no entrar.** Es la misma trampa que con
`--make-launcher` en DigitalOcean: sin una clave propia registrada *antes*, la
máquina se alquila, factura y no te deja pasar. Un comando, dentro del droplet:

```bash
cd ~/src/digital-ocean-dropplet-auto-launching
python3 scripts/vast_instance.py register-key
```

Genera el par si no lo hay y sube la pública. Es idempotente.

### Paso 3 — el barrido

```bash
python3 scripts/vast_instance.py sweep --bench foveal-cpu --cpus 2,4,8,16,32
```

Por cada nivel: busca la oferta más barata, la alquila, sube el código y el
dataset, instala, mide, **recoge el JSON y destruye la máquina**. Antes de
empezar enseña qué va a alquilar y cuánto puede costar como mucho, y pregunta.

Los resultados se guardan en [results/](results/) y se commitean.

### Todo esto desde Telegram

El principio es: **todo va en git menos los `.env`, que salen sólo de tu
laptop.** Los ejecutores del bot viven en
[telegram/executors/](telegram/executors/) de este repo, así que están versionados
junto a los comandos que llaman — si viajaran en el repo del coordinador, una de
las dos mitades quedaría desfasada sin avisar.

Y llegan solos: el coordinador **descubre** los ejecutores de cada repo clonado
(su `data/fuentes.json` trae `~/src/*/telegram`). Con que este repo esté ahí, sus
comandos salen en `/executors`. **No hay paso de instalación ni reinicio.**

| ejecutor | qué hace |
|---|---|
| `actualizar` | `git pull` en todos los repos y reinicia lo que cambió |
| `lanzar` | cualquier subcomando de `do_droplet.py` |
| `vast` | cualquier subcomando de `vast_instance.py` |
| `datos` | cualquier subcomando de `dataset.py` |
| `estado` | droplets **y** instancias de Vast, con su gasto por hora |
| `apagar-vast` | destruye **todas** las instancias de Vast |
| `apagar-do` | destruye los droplets con tag `ephemeral` (**el mini no lo lleva**) |

**El arranque en frío ya no tiene huevo y gallina.** Antes `ejecutores` era un
ejecutor que había que aplicar con `shell` la primera vez; ahora la secuencia
entera es **un** mensaje:

```
actualizar
```

Y luego, para montar la máquina donde Claude va a medir:

```
lanzar   launch bench-control
estado
```

Sí, eso es todo. **Un tipo que se llama igual que el droplet se aplica solo**, y
[types/bench-control.json](types/bench-control.json) ya trae dentro el repo que
clonar, el token que llevarse, `--make-launcher` y el `register-key` de Vast.
`launch` dice qué tipo cogió antes de crear nada, y `--type otro` lo pisa.

Escribir la versión larga desde el móvil es exactamente la clase de cosa que se
teclea mal, y un error de dedo ahí crea una máquina que factura y no sirve.

> **Si vienes de una máquina anterior al 2026-08-22**, los ejecutores que
> `install-executors` copió en su día siguen en `data/executors/` del coordinador
> y **pisan** a los descubiertos. No rompen nada (son los mismos comandos con un
> `cd` de más), pero conviene borrarlos: el arranque del bot y `/executors` dicen
> qué fichero manda y cuál queda pisado.

### Cuando no te acuerdes de un comando

```
/executors           la lista, con el repo que declara cada uno
/executors lanzar    la ficha: qué hace, ejemplos, timeout, dónde está definido
```

La descripción vive en el **mismo JSON** que define el ejecutor (campos
`descripcion` y `ejemplos`), así que no puede divergir. Desde la laptop, el mismo
catálogo:

```powershell
python scripts/do_droplet.py executors
```

que lee `~/src/*/telegram/executors/` de **todos** los repos, no sólo los de éste.

> ⚠️ `apagar-do` se lleva por delante los droplets de trabajo. **Nunca toca el
> mini**, que lleva el tag `control` justamente para eso.

### Los secretos, y sólo ellos, salen de la laptop

Un `.env` no se commitea nunca. Para que una máquina ya creada reciba una
variable **sin perder las que ya tenía**:

```powershell
python scripts/do_droplet.py push-service-env telegram-launcher VAST_AI_API_TOKEN --name mini
```

Reescribe esa línea y conserva el resto, igual que `push-do-token`. Usar
`provision` para esto **borraría del destino lo que tú no tengas a mano**, y el
síntoma llega tarde: algo en esa máquina deja de autenticar sin motivo aparente.

El puente de nombres es el de siempre: `TGL_VAST_AI_API_TOKEN` en tu `.env` llega
como `VAST_AI_API_TOKEN` al del bot. Sin esa variable, un `lanzar launch …
--push-env VAST_AI_API_TOKEN` desde el móvil **crea el droplet igual y sólo
avisa**: nace sin poder alquilar, y lo descubres ya en la máquina.

**Un secreto tiene que ir a dos sitios, y por eso hay dos comandos.** El `.env`
del servicio alcanza **sólo al bot** — el coordinador pasa su entorno a cada
ejecutor, pero una sesión SSH en esa misma máquina no lo tiene. Nos pasó con
`DO_TOKEN` y volvió a pasar con el de Vast: `vast list` funcionaba desde Telegram
y fallaba con "falta el token" entrando por SSH.

```powershell
python scripts/do_droplet.py push-service-env telegram-launcher VAST_AI_API_TOKEN --name mini   # el bot
python scripts/do_droplet.py push-secret VAST_AI_API_TOKEN --name mini                          # la máquina
```

`push-secret` escribe en `~/.config/dev-secrets.env`, que cargan las tres formas
de usar la máquina: sesión interactiva, shell de login y `ssh maquina 'comando'`.
Los dos conservan lo que ya hubiera y repetirlos rota el valor.

### Antes de gastar nada

```bash
python3 scripts/vast_instance.py offers --cpus 8          # qué hay y a cuánto
python3 scripts/vast_instance.py sweep --bench foveal-cpu --dry-run
python3 scripts/vast_instance.py list                     # qué está vivo AHORA
```

`sizes`/`offers` y `--dry-run` son gratis. `list` es el que hay que mirar si algo
se cortó a mitad: una instancia viva factura aunque el proceso que la lanzó ya no
exista.

```bash
python3 scripts/vast_instance.py destroy --all --yes      # el botón de pánico
```

### Tres cosas que conviene saber antes de tocarlo

- **Se alquilan máquinas CON GPU y se usa sólo su CPU.** No es un descuido: en
  Vast.ai las ofertas sin GPU **no son máquinas**, son ofertas de disco
  (`resource_type: "disk"`, `cpu_ram: 0`, 256 núcleos por 0,01 $/h). El nivel de
  CPU sale de `cpu_cores_effective`, que es lo que toca a la porción alquilada.
- **Ningún secreto viaja a una máquina de Vast.** Son ordenadores de
  desconocidos alquilados por minutos. Por eso el código va como un tar por SSH y
  no por `git clone`: clonar exigiría darles un token de GitHub.
- **Un benchmark es un fichero, no código.** [benchmarks/foveal-cpu.json](benchmarks/foveal-cpu.json)
  dice qué enviar, cómo instalar, qué ejecutar y de dónde recoger el número.
  Medir otra cosa es escribir otro JSON, igual que con `services/` y `types/`.

## Coste

Se factura **por segundo mientras el droplet exista**, no por uso. Apagarlo no
para el cobro: hay que **destruirlo**. Los droplets de trabajo se crean con el
tag `ephemeral`, así que un `destroy --tag ephemeral --yes` limpia cualquier
resto olvidado.

Esa limpieza **no toca la máquina de control**, que lleva el tag `control`
justamente para eso, y **así debe seguir siendo**: el mini sólo se destruye
pidiéndolo por su nombre. Ver
[El mini no se destruye nunca en una limpieza](#la-máquina-de-control-lanzar-droplets-desde-el-móvil).

## Qué queda configurado dentro

[cloud-init.yaml](cloud-init.yaml): usuario `deploy` con sudo y tus claves,
autenticación por contraseña desactivada, `ufw` abierto sólo para SSH,
`fail2ban`, y `git`/`curl` instalados. `/var/lib/cloud/READY` aparece cuando el
arranque terminó de verdad (`cloud-init status --wait` para esperarlo).

Y el entorno de desarrollo: **Node 22, Claude Code y `gh`**, más `ripgrep`,
`jq`, `unzip` y `build-essential`. El testigo `/var/lib/cloud/DEV_READY` marca el
final y el log está en `/var/log/dev-tools-install.log`.

Medido en un droplet recién creado: **de `launch` a máquina usable, unos 5
minutos**. Cloud-init tarda 243 s en total, de los cuales 154 s son el
`package_upgrade` de Ubuntu y sólo **30 s** instalar Node, Claude Code y `gh`. Si
alguna vez te sobra ese tiempo, lo que hay que recortar es el `package_upgrade`,
no las herramientas.

Aviso para quien edite `cloud-init.yaml`: **no metas caracteres raros ahí**. Las
minúsculas acentuadas valen; las mayúsculas acentuadas, la raya `—`, las comillas
tipográficas o un `×` hacen que cloud-init descarte el fichero **entero** y el
droplet arranque sin ufw y sin el watchdog de sshd, aparentando estar bien. El
lanzador lo comprueba y se niega a lanzar, pero mejor no llegar ahí. El detalle
está en [CLAUDE.md](CLAUDE.md).

Claude Code se instala **por npm**, no con el instalador nativo
(`curl https://claude.ai/install.sh | bash`). Ese instalador redirige a
`downloads.claude.ai`, que está en Google Cloud Storage y responde
**403 AccessDenied** a la IP del droplet en cuanto haces un par de descargas
(comprobado: 5 intentos seguidos, 5 × 403). Por npm no da problema. Si algún día
cambias esto, pruébalo con un droplet recién creado, no con uno que ya haya
descargado antes.

Además va un **watchdog de sshd**: un timer de systemd que cada minuto comprueba
que alguien escucha en el 22 y, si no, revive `ssh.socket` y lo apunta en
`/var/log/ssh-watchdog.log`. No es paranoia — nos pasó: en Ubuntu 24.04 sshd
arranca por socket, una actualización de `openssh-server` puede devolverlo al
`ssh.service` clásico y en ese cambio se quedó sin arrancar. Un droplet sin sshd
**no se rescata ni por la consola web** (ver abajo).

## Si no puedes entrar

Antes de culpar a tu red, mira **qué puerto responde y cómo**. `ufw` deja pasar
el 22 y el 443 y descarta el resto, y esa asimetría lo delata todo:

| lo que ves | qué significa |
|---|---|
| 22 da **RST** ("conexión rechazada") y el 80 da **timeout** | el paquete llega al droplet: **sshd está caído** |
| todos los puertos igual (todos timeout, o todos RST) | ahí sí, mira la red |

Y ojo con la consola del navegador: **también depende de sshd**, porque el agente
de DigitalOcean se conecta al sshd del propio droplet. Si sshd no escucha, la
única vía es la *Recovery Console* (VNC), que exige contraseña de root — y estas
imágenes se crean sólo con claves, así que hay que resetearla antes desde el
panel. Todo el detalle en
[docs/digitalocean/acceso-ssh-y-consola.md](docs/digitalocean/acceso-ssh-y-consola.md).
