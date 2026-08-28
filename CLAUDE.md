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
11. **Medir hardware con números, y guardarlos en git.** La pregunta es *cuánto acelera un
   entrenamiento si le doy más CPU*, y se responde alquilando varias máquinas, corriendo en
   todas el mismo benchmark congelado y comparando. El resultado no es una conclusión en una
   conversación: es un JSON commiteado en `results/` con la máquina, el coste y el reporte
   entero. Lo que no queda en git no se puede volver a comparar dentro de seis meses.
12. **Un proveedor por lanzador, un objetivo compartido.** DigitalOcean aloja lo de larga vida
   (la máquina de control, los servicios); Vast.ai las máquinas de medir, que viven minutos.
   Los dos scripts no comparten código a propósito, pero sí las reglas: catálogo antes de
   gastar, freno de precio, `list` que enseña lo vivo y destrucción en el camino de fallo.

## Estructura

- [scripts/do_droplet.py](scripts/do_droplet.py) — CLI de todo el ciclo de vida
  (`keygen`, `register-key`, `types`, `sizes`, `launch`, `provision`, `list`, `ssh`,
  `update`, `destroy`). `update` es la excepción: actúa sobre la máquina donde se
  ejecuta, no sobre la API, y se lanza dentro del droplet.
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
- [telegram/executors/](telegram/executors/) — un JSON por **ejecutor del bot**, junto a los
  comandos de este repo que llaman. El coordinador los **descubre** aquí (su
  `data/fuentes.json` trae `~/src/*/telegram`): llegan con `git pull`, sin copiarlos ni
  reiniciar nada. Cada uno lleva su `descripcion` y sus `ejemplos` en el mismo fichero, y
  **no** lleva `cd`: el cwd ya es la raíz de este repo.
- [types/](types/) — un JSON por **tipo de máquina** (`size`, y opcionalmente `image`,
  `region`, `cloud_init`, `tag`, `notas`). Mismo trato que `services/`: **dato, no
  código**, añadir un tipo es añadir un fichero. Se eligen con `--type` o `DO_TYPE`.
- [scripts/vast_check.py](scripts/vast_check.py) — comprueba que `VAST_AI_API_TOKEN`
  funciona **sin alquilar nada**: identidad, saldo, catálogo, benchmarks, instancias
  vivas y claves SSH. Sale 0/1 para poder encadenarlo. Es el primer paso del trabajo
  de comparar GPUs entre proveedores; no toca DigitalOcean ni comparte código con
  `do_droplet.py` (repite `load_env` a propósito: cada script tiene que correr suelto).
- [scripts/vast_instance.py](scripts/vast_instance.py) — el lanzador de Vast.ai:
  `offers`, `register-key`, `launch`, `list`, `ssh`, `destroy`, `bench`, `sweep`.
  Mismo ciclo que `do_droplet.py` contra otra API, y **sin compartir código con él**:
  repite `load_env`, `api()` y los ayudantes de SSH a propósito, para que cada script
  corra suelto en una máquina recién nacida. `sweep` es el comando que justifica el
  fichero: alquila una máquina por nivel de CPU, mide, guarda y destruye.
- [scripts/estado_nubes.py](scripts/estado_nubes.py) — «qué hay vivo AHORA en las dos
  nubes»: la unión de `do_droplet.py list` y `vast_instance.py list`, en **un** sitio.
  No comparte código con ninguno de los dos (objetivo 12): los invoca como procesos, lo
  que además evita que una nube ilegible tape la salida de la otra. Existe porque esa
  unión la piden **dos** ejecutores del bot —`estado` y el `list` de `lanzar`—, y copiada
  en los dos JSON, añadir un proveedor arreglaría uno y dejaría el otro contando de
  menos. Los ejecutores lo llaman con `--exit0`, por lo de siempre: el coordinador lee un
  código != 0 como «el ejecutor falló» y entonces no llega nada al chat.
- [scripts/dataset.py](scripts/dataset.py) — el registro de datos: `list`, `pack`,
  `fetch`, `check`. Resuelve el problema de que **el dato no está en el repo del
  proyecto** y una máquina nueva se queda sin él. Es el único módulo que
  `vast_instance.py` sí importa, porque no es código de un proveedor: es
  infraestructura compartida que viaja siempre con él.
- [datasets/](datasets/) — un JSON por dataset (`destino`, `sha256`, `fuentes`,
  `regenerar`). **Dato, no código.** Cada uno declara varias fuentes y se prueban
  en orden: `repo` (tar.gz commiteado, llega con `git clone` a cualquier
  proveedor), `url` (descarga pública, para lo que no cabe en git) y `local`.
- [benchmarks/](benchmarks/) — un JSON por benchmark (`envia`, `install`, `run`,
  `recoge`, `metrica`). **Dato, no código**, igual que `services/` y `types/`: medir
  otra cosa es escribir otro fichero, no tocar `vast_instance.py`.
- [vast-perfiles/](vast-perfiles/) — un JSON por **conjunto de condiciones de búsqueda**
  en Vast (`cpus`, `min_ram`, `max_price`, `bench`…). En Vast no se pide una máquina por
  su nombre: se busca, y esas condiciones se aprenden pagando. Se aplican con `--perfil`
  en `offers`, `launch`, `bench` y `sweep`; **lo explícito manda sobre el perfil, y el
  perfil sobre el default**, igual que `types/`.
- [results/](results/) — lo medido, commiteado. Un JSON por máquina y una `tabla.md`
  que **se regenera entera** a partir de ellos; no se edita a mano.
- [gpu_training_services.md](gpu_training_services.md) — comparativa de proveedores de
  GPU por **precio y por API**, con tres tablas (capacidades, variedad de GPU/vCPU, y
  el ciclo crear-esperar-medir-destruir endpoint por endpoint). Léelo antes de añadir
  un proveedor nuevo.
- [.env.example](.env.example) — plantilla de configuración; `.env` está ignorado.
- [README.md](README.md) — uso, incluido el flujo multi-máquina.

Droplet por defecto: `s-2vcpu-4gb` (2 vCPU / 4 GB / 80 GB SSD / $24 mes). El disco
no se elige aparte en DigitalOcean; va fijo con el plan.

## Elegir máquina: un tipo no es un `size`

Lo que hay que respetar al tocar esto:

- **Un tipo describe la máquina ENTERA, no sólo su hardware.** Además de `size`, `image`,
  `region`, `cloud_init` y `tag`, puede traer `repos`, `services`, `push_env`,
  `make_launcher`, `volume` y `post`. Las listas se **suman** a lo que venga por línea de
  comandos en vez de pisarse: un `--repo` suelto quiere decir "y además éste", no "olvida
  los del tipo", y pisarlos dejaría la máquina sin la mitad del trabajo sin avisar.
- **Si hay un tipo que se llama como el droplet, se usa.** `launch bench-control` aplica
  `types/bench-control.json`. Es por el móvil: la versión larga
  (`--make-launcher --push-env … --repo …`) se teclea mal, y un error de dedo ahí crea
  una máquina que factura y no sirve. No es magia silenciosa — `launch` dice qué tipo cogió
  antes de crear nada — y `--type otro` lo pisa.
- **`post` son los comandos que rematan la máquina, ejecutados DENTRO al final.** Es donde
  va `vast_instance.py register-key`: el token de Vast deja alquilar pero no entrar, igual
  que el de DigitalOcean, y sin ese paso se alquilan máquinas a las que su creador no puede
  conectarse. Corren con `sudo -u $DEV_USER -H bash -lc` para que vean `dev-secrets.env`;
  con root o sin shell de login fallan con un "falta el token" en una máquina donde el
  token sí está. Ninguno es fatal: la máquina ya está creada y aprovisionada cuando corren.
- **Un tipo es la combinación entera**, no sólo el plan: una GPU necesita ADEMÁS su
  imagen con drivers (`gpu-h100x1-base`, que vale para todos los planes de 1 GPU
  aunque el nombre diga h100) y una región donde haya GPUs. Pedir sólo el `size` da
  una máquina cara con Ubuntu pelado y sin CUDA, que es un fallo que se paga por hora.
- **Las GPU no están en la mayoría de regiones, y eso engaña.** `sizes` filtraba por
  `DO_REGION` (nyc1) y las escondía todas; la conclusión fácil era "mi cuenta no tiene
  GPU". Por eso `--gpu` mira todas las regiones salvo que se pida una, y la línea de
  detalle dice dónde hay cada plan. **No devuelvas ese filtro al valor por defecto.**
- **Que un plan salga en `/v2/sizes` no quiere decir que puedas crearlo.** Son CINCO
  estados distintos, y sólo los cuatro primeros se ven antes de gastar; `comprobar_size()`
  los separa con mensajes que dicen qué escribir después:
  1. no aparece en `/v2/sizes` (o es de contrato, que no se publican ahí);
  2. `available: false` — tu cuenta no lo tiene;
  3. `available: true` pero `regions: []` — **no hay dónde crearlo**. Medido el
     2026-08-16: le pasa a siete planes de GPU, `gpu-l40sx1-48gb` entre ellos. Es
     capacidad, no permisos, y cambia con el tiempo. Un tipo que apunte ahí no arranca nunca;
  4. existe y hay capacidad, pero no en la región que pediste;
  5. **todo lo anterior en orden y aun así 422 al crear: falta cupo de GPU.** Medido el
     2026-08-16 lanzando `gpu-rtx4000` en tor1: `creating this/these droplet(s) will
     exceed your GPU limit`. Este no hay forma de comprobarlo antes — no está en
     `/v2/sizes` (el plan sale disponible y con región) ni en `/v2/account`, que sólo
     trae `droplet_limit`. El cupo se pide en el panel. Por eso `api()` reconoce ese
     mensaje y explica qué es, en vez de soltar el JSON en crudo. **Lo bueno: el 422 es
     un rechazo, no un droplet a medias; no se crea nada ni se factura nada.**
- **Los planes por contrato no salen en `/v2/sizes`.** Para ésos existe `--no-check`;
  no es un atajo para saltarse la validación por comodidad.
- **El precio no se guarda en el descriptor.** Se trae en vivo de `/v2/sizes` cada vez.
  Un número copiado a mano envejece sin avisar, y aquí un número viejo es dinero.
- **El freno de coste (`DO_MAX_PRICE_MONTHLY`, 100 $/mes) es del objetivo 2**, no una
  molestia: desde el móvil un tipo mal escrito se manda igual de rápido que el bueno y
  el error son 3.281 $/mes. `list` enseña por eso el gasto por hora de lo que hay vivo.
- Referencia medida contra la API el 2026-08-16 (no contra la página de precios, que
  redondea a 730 h y no coincide): RTX 4000
  Ada 0,76 $/h ($565,44/mes), RTX 6000 Ada 1,57 $/h ($1.168,08), H100 4,41 $/h
  ($3.281,04/mes), H200 4,47 $/h, MI325X 3,80 $/h. Hay 19 planes de GPU; `sizes --gpu`
  es la lista de verdad. **El mensual no sale de multiplicar el horario**: DigitalOcean
  usa 672 h en la gama basica y 744 h en las de GPU, asi que hay que sumar el
  `price_monthly` de cada plan (con 730 h la suma de dos droplets daba 30,41 en vez de 28,00).
- **Los tipos de `types/` se validan contra la API, no a ojo.** Los dos que se escribieron
  de memoria estaban mal y el `--dry-run` lo destapó: `s-8vcpu-16gb` ya no existe (hoy es
  `s-8vcpu-16gb-amd`) y `gpu-l40sx1-48gb` no tiene capacidad en ninguna región. Antes de
  dar por bueno un tipo nuevo: `launch prueba --type <t> --accept-cost --dry-run`.
- **Lo único sin probar contra una máquina real es el arranque de una GPU, y no por falta
  de intentarlo**: el 2026-08-16 se lanzó `gpu-rtx4000` en tor1 y la API lo rechazó por
  cupo (estado 5 de la lista de arriba). El resto —catálogo, precios, validación,
  `--dry-run` de los siete tipos, `push-do-token`— está comprobado contra la API ese mismo
  día. Lo que sigue pendiente para cuando haya cupo: la imagen de GPU va sobre Ubuntu 22.04
  y `cloud-init.yaml` hace `package_upgrade`, que sobre drivers NVIDIA podría traer un
  kernel nuevo; y el `ssh.socket` de la plantilla es cosa del 24.04, así que en el 22.04
  debería caer al `ssh.service` clásico por el `||` que ya lleva. Anótalo aquí cuando se
  compruebe, en vez de dejarlo en la conversación.

## Documentación de referencia

**[docs/digitalocean/droplets-api.md](docs/digitalocean/droplets-api.md)** — referencia completa y
verificada contra la spec OpenAPI oficial: autenticación y scopes, esquema de `POST /v2/droplets`,
ciclo asíncrono y polling, `user_data`/cloud-init, claves SSH, destrucción, recursos relacionados
(firewalls, VPCs, reserved IPs), recetas en curl/doctl/Python/Node/Terraform y checklist de errores.

**Léela antes de escribir cualquier código que toque la API de DigitalOcean.**

## Lo mínimo que hay que tener presente

> **El reparto mini/dev, con el detalle entero, está en
> [`docs/reparto-mini-dev.md`](docs/reparto-mini-dev.md).** Léelo antes de tocar
> `types/mini.json` o `types/dev.json`.

- **El superviviente tiene que poder apagar todo lo que el desechable encienda.** El
  token de cualquier cosa que **dev** pueda ENCENDER tiene que estar también en el
  **mini**. No para encender: para apagar. dev alquila máquinas que facturan por segundo
  y dev es desechable; cuando muera, el mini es lo único que queda capaz de enumerar y
  matar lo que dejó vivo, y un `apagar-vast` sin `VAST_AI_API_TOKEN` es un botón que no
  hace nada. Si algún día dev enciende algo en un proveedor nuevo, **ese token entra en
  `types/mini.json` en el mismo commit**. Es la regla del freno y el acelerador, aplicada
  a las máquinas.
- **`push_env` es el destino ANCHO de un secreto, no el estrecho.** Escribe en
  `~/.config/dev-secrets.env`, y eso lo ven **el bot y las sesiones SSH**: el unit arranca
  con `ExecStart=/bin/bash -lc` y `provision` pone la línea que lo carga al **principio**
  de `.bashrc`, antes del guard de no-interactiva. El `.env` del servicio (`env_prefix`)
  es el estrecho: sólo el bot. Un token de herramienta (`do_droplet.py`, `vast_instance.py`,
  `gh`) va por `push_env`; uno de configuración del servicio (`BOT_TOKEN`) por el puente.

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
- **Para dar el token a un droplet ya creado está `push-do-token`, no `provision`.**
  `provision` reescribe `dev-secrets.env` entero (`cat >`) a propósito, así que usarlo
  sólo para añadir el token **borra del destino lo que el emisor no tenga a mano** (el de
  Claude, el de GitHub). El síntoma llega tarde y despistado: algo en esa máquina deja de
  autenticar sin motivo aparente. `push-do-token` reescribe una línea y conserva el resto,
  y repetirlo rota el token. Con `--from-env` se manda otro token que no sea el de esta
  máquina; sirve para dar uno de **sólo lectura** a un droplet que sólo tiene que mirar.
- **`authorize-key` corre DENTRO de la máquina donde se quiere entrar**, no contra la API.
  Es la mitad que falta para el SSH entre máquinas: la clave privada no viaja nunca. Ojo
  con a quién se le da: shell en el mini es el token *más* poder destruirlo todo.
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
- **El coordinador YA sabe describir un ejecutor** (desde el 2026-08-22). Su `Executor`
  admite `descripcion` y `ejemplos`, y los imprimen `/executors`, `/executors <nombre>` y
  `/use`. Se acabó el bloque `ayuda` paralelo en el descriptor y el ejecutor `ayuda` que lo
  imprimía: la descripción va en el **mismo fichero** que el ejecutor, así que no hay dos
  sitios que puedan divergir. `do_droplet.py executors` sigue existiendo para consultarlo
  desde la laptop, pero ahora lee `~/src/*/telegram/executors/` de todos los repos.
- **Los ejecutores del bot viven en `telegram/executors/*.json`, aquí, no en el repo del
  coordinador.** Llaman a comandos de este repo; separados, una de las dos mitades queda
  desfasada sin avisar y el síntoma es un ejecutor que falla con un error de argumentos.

  Desde el 2026-08-22 **el coordinador los descubre ahí** (su `data/fuentes.json` trae
  `~/src/*/telegram`), así que llegan con `git pull` y ya está: desde el móvil, sólo
  `actualizar`. No hay paso de aplicación, ni reinicio, ni el huevo y gallina del arranque
  en frío. Antes iban en el bloque `files` del descriptor de `services/` y había que
  copiarlos con `install-executors`, que costaba las cinco cosas que enumera
  [`telegram-coordinator/docs/ejecutores-federados.md`](https://github.com/stalinbeltran/telegram-coordinator/blob/main/docs/ejecutores-federados.md).

  Dos consecuencias al escribir uno:
  - **No lleva `cd`**: el coordinador ejecuta cada comando con el cwd puesto en la raíz del
    repo que lo declara. `python3 scripts/do_droplet.py …` y nada más.
  - **La descripción va en el mismo JSON** (`descripcion`, `ejemplos`), y la imprimen
    `/executors` y `/use` del bot, y `do_droplet.py executors` desde la laptop. Ya no hay
    un bloque `ayuda` aparte que pueda divergir, ni hace falta el ejecutor `ayuda`.

- **Todo lo del mini va en git menos los `.env`.** Es la regla que hace que la máquina se
  pueda tirar y rehacer: el código y los ejecutores se traen solos, y lo único que hay
  que mandar desde la laptop son los secretos, con `push-service-env` (que reescribe una
  línea) y no con `provision` (que reescribe el fichero entero y borra lo que el emisor
  no tenga a mano).
- **Un secreto tiene DOS destinos en la misma máquina, y olvidar uno da un fallo que no
  se entiende.** El `.env` del servicio lo ve sólo el bot (el coordinador pasa su entorno
  a cada ejecutor); `~/.config/dev-secrets.env` lo ven además las sesiones SSH, porque la
  línea que lo carga va al principio de `.bashrc`. Ya mordió con `DO_TOKEN` y volvió a
  morder con el de Vast el 2026-08-20: `vast list` funcionaba desde Telegram y fallaba
  con "falta el token" entrando por SSH a esa misma máquina. Son `push-service-env` y
  `push-secret`, y para un token que use tanto el bot como tú, **hay que mandar los dos**.
- **`--push-env` lee el entorno de la máquina QUE LANZA, no el tuyo.** Desde el mini, eso
  es el `.env` del bot con el prefijo `TGL_` quitado. Si falta la variable, `launch` crea
  el droplet igual y sólo avisa: nace sin poder alquilar y se descubre tarde, ya dentro.
  Por eso `TGL_VAST_AI_API_TOKEN` tiene que estar en el `.env` de la laptop y haberse
  enviado al mini.
- **Las máquinas de larga vida no llevan el tag de los efímeros.** El mini se crea con
  `--tag control` justamente para que `destroy --tag ephemeral --yes` no se lo lleve.
- **NUNCA destruyas el droplet `mini` en una limpieza.** "Borra todos los droplets",
  "limpia lo que quede" o cualquier barrido significan **las máquinas de trabajo**, nunca la
  de control. El mini sólo se destruye si el usuario lo pide **por su nombre y a propósito**
  (`destroy mini`). Si te lo encuentras en una lista que ibas a barrer, exclúyelo y dilo; si
  crees que hay que tocarlo, pregunta antes. Es la máquina desde la que el usuario lanza
  todo cuando no tiene la laptop delante: borrarla estando fuera de casa lo deja sin ninguna
  vía de crear ni destruir droplets, y rehacerla exige volver a la laptop.

## Vast.ai: lo aprendido hasta ahora

Segundo proveedor, para el trabajo de comparar velocidades de GPU. Convive con DigitalOcean,
no lo sustituye. La comparativa razonada está en `gpu_training_services.md`.

- **Su OpenAPI miente en `/api/v0/benchmarks/`.** La especificación documenta `score`, `model`
  y `name`; lo que llega de verdad (medido el 2026-08-20) es `value`, `gpu_name` y `type`, y
  los tres documentados vienen a `null`. Leer `model` daba "0 modelos" **sin que fallara
  nada**: silencioso y creíble, la peor clase de error. Moraleja para el resto de la API:
  **valida contra la respuesta real, no contra la spec.**
- **`POST /api/v0/bundles/` no crea nada, busca.** En esta API el catálogo se consulta con
  POST y se alquila con **`PUT /api/v0/asks/{id}/`**. Al escribir pruebas o herramientas de
  lectura, el POST al catálogo es seguro; el PUT a `/asks/` es lo que cuesta dinero.
- **El catálogo responde SIN autenticar** (comprobado). Permite escribir y depurar todo el
  selector de máquinas antes de tener cuenta. Además da un diagnóstico gratis: si todo falla
  con 401 menos la llamada sin clave, el problema es el token y no la red. `vast_check.py`
  se apoya en eso.
- **Las claves pueden ir con permisos recortados.** Una de sólo lectura autentica, lista el
  catálogo y parece correcta; falla sólo al alquilar. No se puede distinguir sin intentarlo,
  así que **el fallo hay que preverlo en el mensaje**, no descubrirlo.
- **Autenticar no es poder alquilar.** Una cuenta sin saldo pasa todas las comprobaciones de
  token. Por eso `vast_check.py` mira `credit` y lo marca como aviso: si no, el primer
  alquiler falla por un motivo que no se parece en nada a "no tienes dinero".
- **Rate limit de ~3 req/s por endpoint**, con un `429` que dice `API requests too frequent
  endpoint threshold=3.0`. Sin reintento, correr las pruebas dos veces seguidas da un falso
  fallo.
- **`num_gpus: {eq: 0}` NO devuelve máquinas sin GPU: devuelve ofertas de DISCO.** Medido el
  2026-08-20: las 64 que salen traen `resource_type: "disk"`, `cpu_ram: 0` y hasta 256
  núcleos por 0,0103 $/h. Es demasiado bueno para ser verdad porque no es una máquina, es
  almacenamiento, y alquilar una no da nada donde correr. El catálogo (`POST /bundles/`)
  mezcla los dos tipos y **la única señal fiable es `resource_type`**, que ni siquiera es un
  campo por el que se pueda filtrar (`{"resource_type": {"eq": "cpu"}}` da 400). Por eso
  `buscar_ofertas()` filtra en cliente por `resource_type == "gpu"`. **Consecuencia de
  diseño: para medir CPU se alquilan máquinas CON GPU y se usa sólo su vCPU.** No es un
  descuido; en Vast.ai no hay otra forma. Mismo patrón que el `/benchmarks/` de arriba:
  la respuesta no se parece a lo que uno esperaría, y el error es silencioso y creíble.
- **El nivel de CPU es `cpu_cores_effective`, no `cpu_cores`.** El segundo son los núcleos
  del host entero; el primero, los que tocan a la porción alquilada. Medir contra `cpu_cores`
  daría cinco máquinas "distintas" que en realidad reparten el mismo procesador.
- **Un barrido tiene que acotar el nivel por arriba, no sólo por abajo.** Con
  `cpu_cores_effective >= n` la oferta más barata suele tener muchos más núcleos de los
  pedidos: pedir 4 devolvía una de 12. Tres niveles seguidos acababan en máquinas casi
  iguales y el barrido medía tres veces lo mismo **sin decirlo**, que es peor que fallar.
  De ahí el rango `[n, 2n)`. La API acepta dos operadores en el mismo filtro
  (`{"gte": n, "lt": 2n}`), comprobado.
- **A una máquina de Vast no se le da ningún secreto.** No es un droplet tuyo: es el
  ordenador de un desconocido alquilado por minutos, con acceso de root del host a todo lo
  que haya dentro del contenedor. Por eso el código y el dataset viajan como un tar por SSH
  y **no** por `git clone`, que exigiría mandarle un token de GitHub. El objetivo 5 aquí no
  es "nada en `user_data`", es "nada que no sea público, y punto".
- **El catálogo corta en 64 ofertas por consulta** aunque pidas `limit: 1000`. Para barrer
  el mercado hay que trocear la búsqueda por rangos, que es justo lo que hace el barrido al
  ir nivel por nivel.
- **La oferta puede desaparecer entre buscarla y comprarla.** Es un marketplace: `PUT
  /asks/{id}/` contesta 404 o 410 con `no_such_ask` si otro se la llevó primero. No es un
  fallo del programa, y por eso `api()` lo traduce en vez de soltar el JSON.
- **El dato del benchmark no está en el repo del proyecto, y por eso existe `datasets/`.**
  `data/sources/dirty-1000-80px` está gitignoreado en foveal-vision. Antes se empujaba a
  mano y el paso se olvidaba; ahora el dataset se declara, se verifica por sha256 y viaja
  como `tar.gz` commiteado aquí (8,6 MB). **Los datasets se resuelven ANTES de alquilar
  nada**: si falta el dato, el barrido tiene que morir gratis, no con la máquina
  encendida y facturando mientras se depura.
- **El empaquetado de un dataset es determinista a propósito** (orden fijo,
  `uid`/`gid`/`mtime` a cero). Sin eso, dos `pack` del mismo dato dan checksums distintos
  y el sha256 pasa a significar "lo hizo la misma máquina el mismo día" en vez de "es el
  mismo dato", que es justo lo que no sirve. Comprobado el 2026-08-20.
- **Regla de tamaño para un dataset nuevo:** hasta unas decenas de MB, fuente `repo`
  (llega con `git clone`, sin red ni credenciales, a cualquier proveedor). Por encima de
  ~50 MB, publícalo y usa `url`, o el repositorio engorda para siempre. Un volumen de
  bloques de DigitalOcean **no** es una opción aquí: no se conecta a Vast.ai.

## Convenciones

- **Commitea cada cambio, en el momento.** Un cambio lógico, un commit, sin esperar a que el
  usuario lo pida ni acumular varios en uno. Lo que no está commiteado se pierde entre sesiones y
  entre máquinas, que es justo lo que este repo evita.
- **Nunca** commitear tokens ni claves privadas. El token va en `.env` (gitignoreado) o en secrets del CI.
- Variables de entorno: `DIGITALOCEAN_TOKEN` para código propio; `DIGITALOCEAN_ACCESS_TOKEN` es la que
  leen `doctl` y el provider de Terraform.
- No hardcodear slugs de imagen/tamaño/región en el código: van en configuración (`.env` o
  `types/`), y se validan contra `/v2/images`, `/v2/sizes` y `/v2/regions`. El plan lo
  valida `comprobar_size()` en cada `launch`, antes de gastar nada.
- Todo camino de creación debe tener su camino de destrucción, incluido el caso de fallo a mitad
  (una acción `errored` puede dejar un droplet existente e inservible que sigue facturando).
- **Terminado = el comando existe Y se puede invocar desde Telegram.** Si un comando nuevo
  puede empezar o parar un gasto, su ejecutor va en el **mismo commit**: el freno nunca
  llega después del acelerador. El 2026-08-20 hubo 1 h 08 min entre poder alquilar máquinas
  de Vast (`5426f0a`, 20:53) y poder apagarlas desde el móvil (`b35a0cb`, 22:01).
- **Todo número lleva su procedencia**: medido (con fecha y comando) o estimado, dicho en la
  misma línea. Un número sin procedencia se lee siempre como medido, y los tiempos del README
  ya hubo que corregirlos una vez por eso (`b749ce5`).
- **Una trampa se indexa por la acción que la dispara, no por su primera víctima.** «Nos pasó
  con `DO_TOKEN`» se lee como historia y se lee una vez; escrita así, volvió a morder con el
  token de Vast (`d1a9982`). Escríbela como procedimiento: *«al añadir un token nuevo hay que
  mandarlo a sus dos destinos»*.

> El repaso completo de agosto de 2026 —qué se hizo en este repo y en el del coordinador, y
> qué documentación habría ahorrado las vueltas— está en
> [`telegram-coordinator/docs/revision-2026-08-22.md`](https://github.com/stalinbeltran/telegram-coordinator/blob/main/docs/revision-2026-08-22.md).
> Enlazado y no copiado a propósito: una lección duplicada en dos repos es una lección que va
> a divergir.
>
> Pendientes que salen de ahí para **este** repo: un `do_check.py` hermano de `vast_check.py`
> (comprobar contra la API de DigitalOcean lo que el token puede hacer **antes** de gastar —
> los scopes aceptan `volume create` y luego dan 403 al listar), y un mapa de los helpers de
> `do_droplet.py`, que son 128 KB sin índice y ya costaron un `TypeError` por escribir de
> memoria contra la API propia (`f7c2849`).
