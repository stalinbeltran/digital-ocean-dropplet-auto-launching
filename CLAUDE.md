# CLAUDE.md

Contexto para Claude Code al trabajar en este repositorio.

## Qué es este proyecto

Automatización del lanzamiento de **Droplets de DigitalOcean** bajo demanda: crear la máquina,
esperar a que esté operativa, usarla y destruirla. El repo está en fase inicial (aún sin código de
aplicación); lo primero que existe es la documentación de la API.

## Estructura

- [scripts/do_droplet.py](scripts/do_droplet.py) — CLI de todo el ciclo de vida
  (`keygen`, `register-key`, `sizes`, `launch`, `list`, `ssh`, `destroy`).
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
