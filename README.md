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

## Puesta en marcha (esta máquina)

```bash
cp .env.example .env          # y pon tu token dentro
python scripts/do_droplet.py keygen        # par de claves ed25519 dedicado
python scripts/do_droplet.py register-key  # lo sube a tu cuenta de DO
python scripts/do_droplet.py launch
```

`launch` crea el droplet, espera a que la acción termine, espera a que sshd
acepte conexiones y te imprime la IP y el comando de conexión.

## Continuar tus proyectos en el droplet

Cada droplet nuevo arranca con **Claude Code instalado, tu sesión iniciada y tus
repos clonados**, sin tocar nada a mano. La preparación se hace **una sola vez**.

### Preparación (una vez en la vida, no por droplet)

Los dos tokens se sacan **una sola vez** y valen para todos los droplets que
crees después. Si vuelves a esto dentro de unos meses, estos son los pasos
completos.

#### 1. Token de tu suscripción de Claude

Necesitas Claude Code instalado en **tu máquina** (no en el droplet) y una
suscripción activa. En una terminal:

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

#### 2. Token de GitHub (PAT de grano fino)

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
