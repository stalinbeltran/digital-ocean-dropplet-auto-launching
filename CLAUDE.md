# CLAUDE.md

Contexto para Claude Code al trabajar en este repositorio.

## Qué es este proyecto

Automatización del lanzamiento de **Droplets de DigitalOcean** bajo demanda: crear la máquina,
esperar a que esté operativa, dejarla lista para trabajar y destruirla. Todo el ciclo vive en un
único script de Python sin dependencias, más el `cloud-init.yaml` que configura el primer arranque.

## Objetivos del proyecto

Por qué existe esto y contra qué se juzga cualquier cambio. **Esta lista se amplía**: cuando
aparezca un objetivo nuevo, añádelo aquí en vez de dejarlo sólo en la conversación.

1. **De cero a máquina usable en un comando.** `launch` tiene que dejar el droplet creado,
   accesible, con las herramientas instaladas, las credenciales puestas y los repos clonados. Cada
   paso manual que quede es una regresión. Medido hoy: unos 5 minutos.
2. **Efímero de verdad, sin facturas olvidadas.** Todo camino de creación tiene su camino de
   destrucción, incluido el fallo a mitad. Los droplets van etiquetados (`ephemeral`) para poder
   barrerlos con una sola llamada aunque el proceso lanzador muera.
3. **Poder seguir cualquier proyecto en una máquina que acaba de nacer.** El droplet llega con
   Claude Code autenticado, `gh`, git configurado y `~/src` poblado, de modo que continuar un
   trabajo empezado en otra parte no cueste preparación.
4. **Cero fricción en la máquina lanzadora.** Sólo stdlib de Python 3.9+ y `ssh`: sin `pip
   install`, sin entorno virtual, sin Docker. Tiene que arrancar igual en un Windows recién
   formateado que en un Linux.
5. **Ningún secreto en `user_data`.** El `user_data` lo sirve la API de metadatos a cualquier
   usuario del droplet sin sudo. Los tokens viajan después, por SSH y por stdin, a ficheros 600.
6. **El acceso no se puede perder.** Un droplet al que no se entra es un droplet muerto: no hay
   puerta trasera real (la consola web también depende de sshd). De ahí el SSH en 22 y 443, la
   espera al banner en vez de al TCP, el watchdog de sshd y la validación del `cloud-init.yaml`
   antes de enviarlo.
7. **Funcionar desde varias máquinas.** Cada laptop con su propia clave registrada; ninguna clave
   privada viaja.
8. **Documentación que sirve a quien no sabe nada del tema, y verificada.** El README explica de
   dónde sale cada token y cada requisito, y sus comandos se ejecutan antes de darlos por buenos.
9. **Servicios de larga vida, no sólo sesiones interactivas.** El droplet tiene que poder
   alojar procesos que sigan vivos al cerrar el SSH (hoy: el bot de Telegram que permite
   trabajar desde el móvil). El lanzador aloja **cualquier** servicio descrito en `services/`;
   no debe aprenderse ningún proyecto concreto.
10. **Aprendizajes caros, escritos.** Lo que nos ha mordido (el carácter no ASCII que silencia
   cloud-init, el 403 del instalador nativo, el sshd por socket) queda anotado aquí y en `docs/`
   para no volver a pagarlo.

## Estructura

- [scripts/do_droplet.py](scripts/do_droplet.py) — CLI de todo el ciclo de vida
  (`keygen`, `register-key`, `sizes`, `launch`, `provision`, `list`, `ssh`, `update`,
  `destroy`). `update` es la excepción: actúa sobre la máquina donde se ejecuta, no
  sobre la API, y se lanza dentro del droplet.
  **Sólo stdlib a propósito**: debe correr en cualquier máquina con Python 3.9+
  sin `pip install`. No introduzcas dependencias sin motivo fuerte.
- [cloud-init.yaml](cloud-init.yaml) — configuración de primer arranque. El
  lanzador sustituye la línea `# {{SSH_AUTHORIZED_KEYS}}` respetando su sangría.
- [cloud-init.mini.yaml](cloud-init.mini.yaml) — arranque de la **máquina de control** de
  512 MB ($4/mes) que lanza droplets desde el móvil. Sin Claude Code a propósito: es Node y
  ocupa cientos de MB, y los droplets vienen sin swap, así que en 512 MB el kernel lo mata.
  Lleva swapfile de 1 GB. Se elige con `--cloud-init` o `DO_CLOUD_INIT`.
- [services/](services/) — un JSON por servicio de larga vida (`repo`, `install`, `start`,
  `env_prefix`). Es **dato, no código**: añadir un servicio nunca debe requerir tocar
  `do_droplet.py`. Se activan con `DO_SERVICES` o `--service`.
- [.env.example](.env.example) — plantilla de configuración; `.env` está ignorado.
- [README.md](README.md) — uso, incluido el flujo multi-máquina.

Droplet por defecto: `s-2vcpu-4gb` (2 vCPU / 4 GB / 80 GB SSD / $24 mes). El disco
no se elige aparte en DigitalOcean; va fijo con el plan.

## Documentación de referencia

**[docs/digitalocean/droplets-api.md](docs/digitalocean/droplets-api.md)** — referencia completa y
verificada contra la spec OpenAPI oficial: autenticación y scopes, esquema de `POST /v2/droplets`,
ciclo asíncrono y polling, `user_data`/cloud-init, claves SSH, destrucción, recursos relacionados
(firewalls, VPCs, reserved IPs), recetas en curl/doctl/Python/Node/Terraform y checklist de errores.

**Léela antes de escribir cualquier código que toque la API de DigitalOcean.**

## Lo mínimo que hay que tener presente

- Base URL `https://api.digitalocean.com/v2/`, auth `Authorization: Bearer $DIGITALOCEAN_TOKEN`.
- `POST /v2/droplets` devuelve **202 Accepted**, no un droplet listo. Requeridos: `size` e `image`
  (más `name` o `names`, máx. 10).
- El flujo correcto es: crear → pollear `GET /v2/actions/{id}` hasta `completed` → `GET /v2/droplets/{id}`
  para leer la IP pública en `networks.v4[].type == "public"` → reintentar SSH hasta que conecte.
- `status: "active"` **no** implica que sshd esté escuchando ni que cloud-init haya terminado.
- Rate limit: 5.000 req/hora y 250 req/minuto por token. Backoff exponencial en el polling.
- Etiqueta siempre los droplets efímeros (`tags`) para poder limpiarlos con una sola llamada
  (`DELETE /v2/droplets?tag_name=...`) aunque el proceso lanzador muera.
- `private_networking` está deprecado; usa `vpc_uuid`.

## cloud-init.yaml: cuidado con los caracteres no ASCII

**Un solo carácter mal elegido en un comentario deja el droplet sin ninguna
configuración, y sin avisar.** Nos pasó con un `x` de multiplicar (`5 × 403`).

Por el camino hasta el droplet el `user_data` acaba releyéndose como latin-1. Un
carácter cuya codificación UTF-8 lleve un byte entre **0x80 y 0x9F** se convierte
entonces en un carácter de control C1, y el parser de YAML de cloud-init rechaza
**el fichero entero**:

```
Failed loading yaml blob. unacceptable character #x0097
Failed at merging in cloud config part from part-001: empty cloud config
Skipping modules '...,runcmd' because no applicable config is provided
```

El droplet arranca igualmente, coge IP y deja entrar a root por SSH — o sea, que
*parece* correcto — pero **sin usuario `deploy`, sin ufw, sin el 443 y sin el
watchdog de sshd**, que es precisamente la avería de la que no se vuelve.

**Esto aplica sólo a `cloud-init.yaml`**, que es lo único que viaja como
`user_data`. En el README, en este fichero, en el código Python y en los mensajes
de commit escribe lo que quieras: no pasan por ese camino.

Dentro de `cloud-init.yaml`:

| | |
|---|---|
| **Seguro** | ASCII; minúsculas acentuadas `á é í ó ú ñ ü`; `¿ ¡ º ª` |
| **Rompe** | MAYÚSCULAS acentuadas `Á É Í Ó Ú Ñ`; `×`; raya `—` y `–`; comillas tipográficas `“ ” ‘ ’`; `…`; flechas `→`; emoji |

La regla real es "ningún byte entre 0x80 y 0x9F en la codificación UTF-8", pero
en la práctica basta con recordar que **las minúsculas acentuadas valen y casi
todo lo demás no ASCII, no**. Si dudas, escribe ASCII y ya.
- `build_user_data()` llama a `check_user_data_encoding()`, que comprueba esto
  antes de enviar nada y se niega a lanzar indicando línea y carácter. **No
  quites esa comprobación**: el fallo es silencioso y caro de encontrar.
- Al depurar un droplet que "arrancó pero le falta todo", mira siempre
  `grep -i "yaml blob" /var/log/cloud-init.log` antes que ninguna otra cosa.

## Acceso SSH: lo que ya nos ha mordido

Detalle y reproducciones en
[docs/digitalocean/acceso-ssh-y-consola.md](docs/digitalocean/acceso-ssh-y-consola.md).
Lo imprescindible:

- **Antes de culpar a la red, compara un puerto permitido con uno denegado.** ufw deja pasar el 22
  y el 443 y tira el resto. Si el 22 contesta **RST** y el 80 se queda en **timeout**, el paquete
  llega al droplet y lo que pasa es que **nadie escucha**: sshd está caído, no hay bloqueo de red.
  Si *todos* los puertos se comportan igual, entonces sí mira la red. Un RST con ~2,6 s de retardo
  parece un appliance y no lo es.
- **La consola web de DigitalOcean no es una puerta trasera: va por encima de sshd.** Si sshd no
  escucha, tampoco entras por ahí. La única vía sin sshd es la *Recovery Console* (VNC), y **exige
  contraseña**, que estas imágenes no tienen: hay que resetear la de root desde el panel.
- **El agente de DO elige el puerto leyendo el primer `Port` de `/etc/ssh/sshd_config`**, no
  `ssh.socket`. Si cambias puertos, cámbialos en los dos sitios o la consola web apuntará al 22 a
  ciegas.
- **En Ubuntu 24.04 sshd va por socket** (`ssh.socket`), así que `Port` de `sshd_config` se ignora
  para escuchar; los puertos reales salen de `ListenStream`. Una actualización de `openssh-server`
  puede devolverlo al `ssh.service` clásico, y en ese vaivén se ha quedado sin arrancar. Por eso
  `cloud-init.yaml` instala el timer `ssh-watchdog`, que cada minuto comprueba que algo escucha en
  el 22 y revive sshd dejando traza en `/var/log/ssh-watchdog.log`. **Si tocas el arranque de sshd,
  no quites el watchdog**: sin él un droplet sin sshd es irrecuperable salvo a mano por VNC.
- **Todo `ssh` del lanzador va con keepalives, y toda espera con `timeout=`.** `runcmd`
  reinicia `ssh.socket` en pleno arranque, así que una conexión abierta en ese momento se
  queda medio abierta: el cliente espera para siempre a un servidor que ya no está. Nos
  colgó un `launch` 20 minutos con el droplet perfectamente listo — `DEV_READY` puesto y la
  sonda de `wait_for_dev_tools` esperando a un `ssh` muerto. Como `subprocess.run` no
  llevaba `timeout`, el `deadline` de la propia función no llegaba a comprobarse nunca.
  De ahí `ServerAliveInterval`/`ConnectTimeout` en `ssh_command()` y el `timeout=` en la
  sonda. **No los quites**: el síntoma es "se quedó pensando" y no aparece en ningún log.
- **El borrado es asíncrono.** `DELETE /v2/droplets/{id}` contesta enseguida pero el droplet sigue
  saliendo en `GET /v2/droplets` unos segundos. Destruir y recrear con el mismo nombre sin esperar
  falla con un "ya existe" falso; `cmd_destroy` espera con `wait_until_gone()`.

## Entorno de desarrollo dentro del droplet

El objetivo es poder seguir cualquier proyecto en una máquina recién creada sin
trabajo manual. cloud-init instala las herramientas; `do_droplet.py provision`
(que `launch` llama solo) inyecta las credenciales después.

- **Claude Code se instala por npm, no con el instalador nativo.**
  `curl https://claude.ai/install.sh | bash` redirige a `downloads.claude.ai`
  (Google Cloud Storage), que devuelve **403 AccessDenied** a la IP del droplet
  tras las primeras descargas — medido: 5 intentos seguidos, 5 × 403. Por npm
  (`npm i -g @anthropic-ai/claude-code`, global como root → `/usr/bin/claude`)
  funciona y lo ven todos los usuarios sin tocar el PATH. Si vuelves a probar el
  instalador nativo, hazlo en un droplet **recién creado**: uno que ya haya
  descargado antes te dará un falso positivo.
- **Ningún secreto puede ir en `cloud-init.yaml`.** El `user_data` lo sirve la
  API de metadatos y lo lee cualquier usuario sin sudo:
  `curl http://169.254.169.254/metadata/v1/user-data` devuelve el YAML entero.
  Comprobado desde `deploy`. Los tokens van después, por SSH y **por stdin**
  (no como argumento de `ssh`, que saldría en el `ps` del droplet), a ficheros
  en modo 600 del usuario de desarrollo.
- **La línea que carga los tokens se antepone a `.bashrc`**, por delante del
  corte que Ubuntu pone para shells no interactivas. Sin eso,
  `ssh droplet 'claude -p ...'` se queda sin token. Verificado en los tres
  casos: sesión interactiva, shell de login y comando remoto.
- **`provision` entra siempre como root**, sea cual sea `DO_SSH_USER`: tiene que
  escribir en el home de otro usuario y hacer `chown`. Para el uso diario sí
  conviene `DO_SSH_USER=deploy`, que es donde están las credenciales y `~/src`.
- Autenticación: `CLAUDE_CODE_OAUTH_TOKEN` (suscripción, sale de
  `claude setup-token` una vez) o `ANTHROPIC_API_KEY` (factura por uso). Se
  comprueba cuál está activa con `claude auth status`, que responde JSON.
- El testigo `/var/lib/cloud/DEV_READY` marca que las herramientas ya están;
  `DEV_FAILED` que la instalación falló, para no esperar en balde. Log en
  `/var/log/dev-tools-install.log`.

## Servicios en el droplet

Los instala `provision` como unidades de systemd, después de las credenciales y los repos.
Lo que hay que respetar:

- **`ExecStart` va con `bash -lc`, no directamente el comando.** El proceso necesita los
  tokens de `~/.config/dev-secrets.env`, y systemd **no** puede leer ese fichero con
  `EnvironmentFile`: sus líneas llevan `export`, que `EnvironmentFile` no admite. El shell
  de login sourcea `.profile` → `.bashrc`, donde `provision` puso la línea que lo carga.
  Sin esto el servicio arranca y `claude` responde "no autenticado", que despista mucho.
- **`WorkingDirectory` es obligatorio.** Casi todo servicio busca su `.env` y sus datos
  relativos al cwd (el coordinador de Telegram, sin ir más lejos). Se sustituye con `sed`
  sobre `@DIR@` en vez de expandirlo en el heredoc, porque el home real sólo se conoce ya
  en el droplet y expandir ahí afectaría también al comando de arranque del descriptor.
- **Ningún fallo de un servicio aborta el aprovisionamiento.** Para cuando corren, las
  credenciales y los repos ya están puestos; tumbar todo por un `npm ci` sale peor. Avisan y
  siguen.
- **La configuración del servicio no puede ir en su repo ni en cloud-init.** Suele ser
  secreta (el token del bot lo es). Va por el puente `env_prefix`: `TG_BOT_TOKEN` aquí es
  `BOT_TOKEN` allí, en modo 600, empujado por SSH como el resto.
- **Un droplet con servicio es de larga vida**, lo que roza el objetivo 2: no lo barras con
  `destroy --tag ephemeral` a ciegas.
- Con el coordinador de Telegram, **sólo puede haber una instancia haciendo polling**: si
  también corre en la laptop, Telegram devuelve 409 a una de las dos. Por eso la máquina de
  control lleva un **bot distinto** (`telegram-launcher`, prefijo `TGL_`) y no el mismo: no
  es una cuestión de comodidad, con un solo token uno de los dos se queda fuera. Y por eso
  `selected_services()` se niega a instalar juntos dos servicios del mismo repo.
- **El `DO_TOKEN` del mini va por `--push-do-token`**, que lo escribe en
  `~/.config/dev-secrets.env` con el resto de secretos. Es una opción de línea de comandos
  y **no** una variable del `.env` a propósito: así no se cuela en todos los droplets.
  El `TGL_DO_TOKEN` del `.env` del bot también funciona (el coordinador pasa su entorno a
  cada comando, `runner.ts`: `{...process.env}`), pero **sólo alcanza al bot**: entrando por
  SSH a la máquina, `do_droplet.py` no veía el token y `register-key` fallaba con un "falta
  el token" que no se entiende. Con ello, quien pueda hablarle a ese bot o entrar a esa
  máquina puede gastar dinero en la cuenta: la allowlist es la única barrera.
- **Un servicio no se entera de que su repo cambió.** El código está cargado en el
  proceso desde que arrancó: `git pull` sin `systemctl restart` deja al servicio
  corriendo lo viejo, y no hay ningún síntoma que lo delate salvo que el arreglo
  "no funciona". De ahí `do_droplet.py update`, que corre **dentro** del droplet
  (por SSH o desde el ejecutor `actualizar` del bot), hace el pull en cada repo de
  `~/src` y reinicia sólo los servicios cuyo `WorkingDirectory` apunta a un repo
  que cambió. `npm ci` únicamente si el pull tocó `package.json` o el lock: en 512
  MB uno de más son minutos con el servicio parado.
- **Un servicio que se reinicia a sí mismo se corta la respuesta.** Cuando el
  update lo pide el bot, quien lo ejecuta es un hijo del bot: `systemctl restart`
  mata el cgroup entero, con ese proceso y el mensaje que aún no había salido
  hacia Telegram. El reinicio propio se programa con `systemd-run --on-active=3`,
  que crea una unidad transitoria **fuera** del cgroup y sobrevive. Un
  `sleep && systemctl restart` en segundo plano no vale: muere con el servicio.
- **Para saber qué servicios instaló `provision`, pregunta a systemd.**
  `build_provision_script` corre con `umask 077`, así que las unidades quedan en
  modo 600 de root y `deploy` —el usuario del bot y de los repos— no puede
  leerlas. Buscar la marca leyendo `/etc/systemd/system/*.service` daba lista
  vacía y un "no hay nada que reiniciar" falso; `systemctl show` contesta a
  cualquier usuario porque responde el gestor, no el fichero.
- **Las máquinas de larga vida no llevan el tag de los efímeros.** El mini se crea con
  `--tag control` justamente para que `destroy --tag ephemeral --yes` no se lo lleve.
- **NUNCA destruyas el droplet `mini` en una limpieza.** "Borra todos los droplets",
  "limpia lo que quede" o cualquier barrido significan **las máquinas de trabajo**, nunca la
  de control. El mini sólo se destruye si el usuario lo pide **por su nombre y a propósito**
  (`destroy mini`). Si te lo encuentras en una lista que ibas a barrer, exclúyelo y dilo; si
  crees que hay que tocarlo, pregunta antes. Es la máquina desde la que el usuario lanza
  todo cuando no tiene la laptop delante: borrarla estando fuera de casa lo deja sin ninguna
  vía de crear ni destruir droplets, y rehacerla exige volver a la laptop.

## Convenciones

- **Commitea cada cambio, en el momento.** Un cambio lógico, un commit, sin esperar a que el
  usuario lo pida ni acumular varios en uno. Lo que no está commiteado se pierde entre sesiones y
  entre máquinas, que es justo lo que este repo evita.
- **Nunca** commitear tokens ni claves privadas. El token va en `.env` (gitignoreado) o en secrets del CI.
- Variables de entorno: `DIGITALOCEAN_TOKEN` para código propio; `DIGITALOCEAN_ACCESS_TOKEN` es la que
  leen `doctl` y el provider de Terraform.
- No hardcodear slugs de imagen/tamaño/región en el código: van en configuración, y se validan contra
  `/v2/images`, `/v2/sizes` y `/v2/regions`.
- Todo camino de creación debe tener su camino de destrucción, incluido el caso de fallo a mitad
  (una acción `errored` puede dejar un droplet existente e inservible que sigue facturando).
