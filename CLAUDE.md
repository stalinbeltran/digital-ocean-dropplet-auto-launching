# CLAUDE.md

Contexto para Claude Code al trabajar en este repositorio.

## Qué es este proyecto

Automatización del lanzamiento de **Droplets de DigitalOcean** bajo demanda: crear la máquina,
esperar a que esté operativa, usarla y destruirla. El repo está en fase inicial (aún sin código de
aplicación); lo primero que existe es la documentación de la API.

## Estructura

- [scripts/do_droplet.py](scripts/do_droplet.py) — CLI de todo el ciclo de vida
  (`keygen`, `register-key`, `sizes`, `launch`, `provision`, `list`, `ssh`, `destroy`).
  **Sólo stdlib a propósito**: debe correr en cualquier máquina con Python 3.9+
  sin `pip install`. No introduzcas dependencias sin motivo fuerte.
- [cloud-init.yaml](cloud-init.yaml) — configuración de primer arranque. El
  lanzador sustituye la línea `# {{SSH_AUTHORIZED_KEYS}}` respetando su sangría.
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

- Las minúsculas acentuadas se salvan de casualidad: `á` es `0xC3 0xA1` y su
  segundo byte pasa de 0x9F.
- Las **mayúsculas acentuadas** (`Á` = `0xC3 0x81`, `Ñ` = `0xC3 0x91`) y la
  **raya** (`—` = `0xE2 0x80 0x94`) sí rompen. Y este repo escribe rayas por
  todas partes: en `cloud-init.yaml` usa guiones normales.
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
