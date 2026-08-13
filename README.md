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
| `DO_SIZE` | No — `s-2vcpu-4gb` | Plan de la máquina. Míralos con `sizes` |
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
  --push-do-token
```

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
python scripts/do_droplet.py launch --no-provision   # crudo, sin credenciales
python scripts/do_droplet.py provision         # reinyectar credenciales y repos
python scripts/do_droplet.py list              # qué hay vivo
python scripts/do_droplet.py ssh               # conectar
python scripts/do_droplet.py ip                # sólo la IP
python scripts/do_droplet.py service logs telegram-coordinator  # log de un servicio
python scripts/do_droplet.py destroy           # destruir (pide confirmación)
python scripts/do_droplet.py destroy --tag ephemeral --yes   # limpieza total
```

Antes de gastar nada, comprueba qué se va a enviar:

```bash
python scripts/do_droplet.py launch --dry-run
```

Descubrir slugs vigentes en lugar de fiarte de los del `.env`:

```bash
python scripts/do_droplet.py sizes --region nyc1
python scripts/do_droplet.py regions
python scripts/do_droplet.py images --filter ubuntu
```

## Coste

Se factura **por segundo mientras el droplet exista**, no por uso. Apagarlo no
para el cobro: hay que **destruirlo**. Todos se crean con el tag `ephemeral`,
así que un `destroy --tag ephemeral --yes` limpia cualquier resto olvidado.

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
