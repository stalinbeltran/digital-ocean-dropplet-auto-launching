# CLAUDE.md

Contexto para Claude Code al trabajar en este repositorio.

## Qué es este proyecto

Automatización del lanzamiento de **Droplets de DigitalOcean** bajo demanda: crear la máquina,
esperar a que esté operativa, dejarla lista para trabajar y destruirla. Todo el ciclo vive en un
único script de Python sin dependencias, más el `cloud-init.yaml` que configura el primer arranque.

## ⚠ Este repo es una pieza de un sistema de seis, y el CENTRAL es otro

Lo que **no es de ningún repo en concreto** —los reportes de todos los estudios, qué está
decidido y qué sigue abierto, y qué pieza hace qué— vive en
[`estudios-redes-neuronales`](https://github.com/stalinbeltran/estudios-redes-neuronales).
Se enlaza, no se copia.

| Si quieres saber… | Mira en |
|---|---|
| **qué está fijado hoy** y qué sigue abierto | [`ESTADO.md`](https://github.com/stalinbeltran/estudios-redes-neuronales/blob/main/ESTADO.md) |
| **qué se corrió, cuándo y qué costó** | [`reportes/README.md`](https://github.com/stalinbeltran/estudios-redes-neuronales/blob/main/reportes/README.md) |
| **qué repo hace qué** | su [`README.md`](https://github.com/stalinbeltran/estudios-redes-neuronales/blob/main/README.md) |
| **qué variables pide cada proyecto** y cuáles son secretos de verdad | [`docs/secretos-inventario.md`](https://github.com/stalinbeltran/estudios-redes-neuronales/blob/main/docs/secretos-inventario.md) |
| **cómo conseguir todos los tokens desde cero** si se pierden | [`docs/secretos-desde-cero.md`](https://github.com/stalinbeltran/estudios-redes-neuronales/blob/main/docs/secretos-desde-cero.md) |

⚠ **Y si algo que se hace aquí termina en un estudio o una medición, su reporte va allí**, no
aquí — sea cual sea el repo desde el que se lanzó. Un reporte guardado en el repo que lo dispara
es invisible para quien clona otro.

⚠ **Y lo que se pide y lo que se va descubriendo, según pasa, va a la
[bitácora](https://github.com/stalinbeltran/estudios-redes-neuronales/tree/main/bitacora)** de ese
mismo repo, aunque el trabajo sea de aquí y aunque no acabe en ningún estudio. **Sólo se añaden
filas; una escrita no se toca nunca más**, ni para corregirla: si resultó falsa, se añade otra que
lo diga. Existe por lo del 2026-09-11, que está en su primera entrada: reconstruir qué se había
pedido la tarde anterior costó una mañana, porque los commits cuentan el resultado y no el camino.

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
  (`keygen`, `register-key`, `clave-flota`, `autorizar-flota`, `types`, `sizes`,
  `launch`, `provision`, `list`, `ssh`, `remoto`, `flota`, `llavero`, `entornos`,
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
- [llavero.json](llavero.json) — los secretos que lleva **cualquier** máquina de la
  flota, con `obligatoria` y `porque` por variable. **Dato, no código**: añadir un
  secreto es añadir una línea. Un tipo lo pide con `"llavero": true`.
- [entornos/](entornos/) — un JSON por **proyecto cuyo `.env` se genera** del llavero
  (`dir`, `fichero`, y por variable `nombre`/`desde`). **Dato, no código.** Es lo que
  hace que los `.env` de varios proyectos no haya que copiarlos entre máquinas.
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

> **mini y dev son la MISMA máquina en dos tallas, no dos clases con permisos
> distintos.** El diseño y la prueba de aceptación están en
> [`docs/flota-simetrica.md`](docs/flota-simetrica.md) — **implementado y probado
> end-to-end el 2026-09-10**. Léelo antes de tocar `types/mini.json` o `types/dev.json`.
> [`docs/reparto-mini-dev.md`](docs/reparto-mini-dev.md) describe los ROLES; su tabla de
> «quién puede qué» quedó superada.
>
> Las **únicas tres diferencias**, y ninguna es de permisos: la **talla** (y de ahí que
> el mini no lleve Claude Code, que en 512 MB lo mata el kernel), el **tag** (`control`
> contra `ephemeral`, que es qué se barre) y el **bot** (Lanzador contra Coordinador,
> que es obligatorio: Telegram devuelve **409** al segundo proceso que haga polling con
> el mismo token).

- **Si una variable hace falta para CREAR una máquina de la flota, va en el llavero.**
  [`llavero.json`](llavero.json) declara lo que lleva **cualquiera** de las dos; un tipo
  lo pide con `"llavero": true`, y sólo lo piden `mini` y `dev`. Falta una obligatoria y
  `launch`/`provision` **mueren antes de crear ni tocar nada** — no avisan: mueren.
  `--sin-llavero` es la salida de emergencia.
  Esta regla **sustituye** a la vieja del freno y el acelerador («el token de lo que dev
  pueda encender va también en el mini, para poder apagarlo»), que era un caso particular
  y se quedaba corta: no cubría las variables que no encienden nada y sin las cuales la
  máquina nace coja. Lo que costó descubrirlo, medido el 2026-09-10: el mini llevaba
  `TG_*` y no `TGL_*`, o sea que sabía parir un dev y **no sabía parir un mini**; y de sus
  19 variables `types/mini.json` declaraba **una**, así que no se reconstruía del repo.
- **El acceso entre máquinas NO puede depender del orden de nacimiento.** Un droplet
  acepta las claves registradas **en el momento de crearlo**, así que una clave por
  máquina significa que la que nace después nunca entra en la que nació antes: medido el
  2026-09-10, el dev **nunca** había podido entrar en el mini, y no por un olvido. De ahí
  la **clave de flota**: una sola, registrada una vez (`clave-flota`), que llevan todas.
  `hacer_lanzador()` la reusa en vez de generar un par nuevo — antes registraba uno por
  dev y había **26 `lanzador-dev` muertas** de 31 claves en la cuenta.
  ⚠ `autorizar-flota` manda la pública **y la privada**, y las dos hacen falta: una
  máquina que sólo deja entrar no es un par. Y la pública va a **root además de** al
  usuario de desarrollo, porque todo el aprovisionamiento entra como root.
- **Lo que actúa DENTRO de una máquina se puede pedir desde fuera con `remoto`.**
  `remoto mini update`, `remoto dev install-service --service X`. Corre como el usuario
  de desarrollo y con `bash -lc`; sin el shell de **login** no se carga `dev-secrets.env`
  y el comando de allí falla con un «falta el token» en una máquina donde el token sí
  está.
- **La paridad se comprueba, no se supone: `flota`.** Dice qué le falta a cada máquina
  viva del llavero, si tiene la clave de flota y qué servicios corre. Sólo **nombres**,
  ningún valor, y sale `!= 0` si falta algo obligatorio — que es lo único que hace que el
  aviso llegue al chat de Telegram.
- **⚠ CUALQUIER máquina de la flota puede destruir a las demás, y NADA lo impide.**
  Comprobado el 2026-09-10 destruyendo el mini de verdad desde un dev: `destroy mini
  --yes` funcionó a la primera. `cmd_destroy` sólo pide `--yes`; la regla «nunca
  destruyas el mini» vive **sólo en esta documentación** y no hay una línea de código que
  la aplique. Antes lo hacía improbable que el token estuviera en pocos sitios; con la
  paridad está en todas, así que **la regla ahora depende enteramente de que quien lea
  esto la respete**.
- **Rehacer el mini funciona, y cuesta una IP.** El dev lo destruyó y lo volvió a crear
  con el mismo nombre, con su llavero y su bot. Pero **la IP cambia** (medido: de
  `67.205.158.85` a `159.89.83.184`), así que todo lo que apunte a la vieja deja de
  resolver. Lo que NO se pierde: nada del llavero, porque está declarado y viaja; lo que
  hubiera que no esté en `llavero.json` **sí** se pierde, y ésa es la razón de que el
  llavero exista.
- **Un `.env` de proyecto se GENERA del llavero, no se copia entre máquinas.**
  [`entornos/`](entornos/), un JSON por proyecto, con `desde` (nombre en el llavero) y
  `nombre` (dentro del proyecto). **El nombre de destino lo declara quien lo CONSUME**,
  no quien lo transporta: `CWEB_TS_AUTHKEY` aquí es `TS_AUTHKEY` allí porque así lo lee
  `tailscale-unir.mjs`. Copiar en vez de generar exigiría saber cuál de las dos copias es
  la buena, y **las fechas mienten**: el `.env` de un dev recién nacido es el más nuevo y
  el más vacío.
  ⚠ Y antes de escribir un `.env` se le pregunta a **git** si lo ignora; si no lo ignora,
  **no se escribe**. Es estructural y no disciplinaria: falla en el momento del error y
  no en el `git push` de tres semanas después.
- **Mover secretos entre máquinas: `llavero comparar|enviar|traer|olvidar`.** `traer`
  sólo **añade** (nunca pisa un valor local salvo `--pisar`), `enviar` sólo **mezcla**, y
  borrar es un comando **aparte** — para que borrar no pueda ser efecto secundario de
  sincronizar. No hay `sync` a propósito: uno que decide solo es uno que un día decide
  mal, y con secretos eso no se nota hasta que algo deja de autenticar.
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

## Esperar al arranque: una espera que no cuenta nada no se puede depurar

El 2026-09-10 por la noche dos `launch dev` seguidos desde el mini murieron con **«Se agotó la
espera a que el droplet terminase de instalar las herramientas»** y nada más. Encontrarlo costó
una mañana entera y dos droplets lanzados a mano —que salieron bien, en 272 s y 348 s—, y aun
así la conclusión honesta fue *no se sabe*: el bucle no había guardado un solo dato. Lo que se
aprende de ahí, y aplica a **cualquier** espera que se escriba en este repo:

- **Una sonda por SSH que falla deja `stdout` vacío, que es idéntico a un «todavía no».** Esos
  dos casos —la máquina va lenta y no llego a la máquina— no se pueden confundir, porque el
  arreglo de cada uno no se parece en nada. Se distinguen mirando el `stderr` y el código de
  salida, que antes se tiraban a la basura.
- **El diagnóstico va DENTRO del mensaje de error, no impreso por el camino.** Cuando el
  lanzamiento sale del bot, el coordinador sólo publica `stderr` y sólo si el código no es 0:
  todo lo que se cuente por `stdout` mientras se espera no llega a ningún sitio. De ahí
  `diagnostico_de_arranque()`, que pregunta a la máquina qué está haciendo justo antes de morir
  —`cloud-init status`, si hay un apt peleando, y las últimas líneas del log de instalación— y
  lo mete en el `die()`.
- **Y el error avisa de que el droplet SIGUE VIVO Y FACTURANDO**, con cómo rematarlo
  (`provision`) y cómo tirarlo (`destroy`). Ayer nadie lo dijo, y quedaron dos máquinas de
  24 $/mes encendidas sin que hicieran falta.
- **El plazo era de 900 s y se quedó corto dos veces.** Hoy sale de `DO_DEV_TOOLS_TIMEOUT`
  (1800 s por defecto). Esperar de más cuesta céntimos; relanzar cuesta el lanzamiento entero.
- **La duración del arranque es una lotería, y la echa `apt-daily`.** Los `apt-get install` del
  arranque piden el cerrojo de dpkg, y el timer de las actualizaciones automáticas de Ubuntu
  lleva un retardo **aleatorio de hasta 12 h**: unas veces cae en el primer arranque y otras no
  (medido el 2026-09-11 en dos droplets recién creados: el siguiente disparo salía a las 23:36
  en uno y a la 01:46 del día siguiente en el otro). Si caen juntos, cloud-init espera lo que
  haga falta. Por eso las dos plantillas paran esos timers en `bootcmd` y los devuelven al final
  de `runcmd`: **lo que se desactiva es la carrera, no las actualizaciones**, y la ventana es
  exactamente el aprovisionamiento. `systemctl stop` de un timer no es persistente.
- **Y un arranque lentísimo que ACABA BIEN también se denuncia.** Subir el plazo dejó de perder
  lanzamientos, pero de paso convertía el arranque patológico en un éxito mudo: antes fallaba y
  al menos se notaba. Por encima de `LENTO_SOSPECHOSO` (600 s, el doble de lo peor medido) la
  espera dice cuánto tardó y adjunta el diagnóstico. Va por `stdout` a propósito: en un `launch`
  que sale con 0, eso es justo lo que el coordinador publica en el chat.
- **Y cuando la espera se agota, el error dice que la máquina está A MEDIO HACER.** `launch`
  muere ahí, que es **antes** de `provision`: sin secretos, sin repos y sin servicios. El
  2026-09-10, tras el fallo, se le escribió al bot de ese dev y no contestó nunca, y eso se leyó
  como «además se rompió algo». No se había roto nada: el bot no estaba instalado todavía.
- **⚠⚠ Y LA CAUSA DE VERDAD, encontrada el 2026-09-11 en vivo: el entorno de un servicio es una
  FOTO de cuando arrancó.** `provision` instala y ARRANCA los servicios, y `hacer_lanzador()` y
  los `post` del tipo escriben en `dev-secrets.env` **después**. Todo lo que se escriba a partir
  de ahí es invisible para ese servicio **para siempre**, y nada lo delata.
  Lo concreto: un mini recién hecho arranca su bot antes de que exista
  `DO_SSH_KEY_FILE=~/.ssh/do_flota`, así que **todo `launch` que salga del bot** cae al defecto
  `~/.ssh/do_droplet` —una clave local que **nadie registró en la cuenta**— y se queda sondeando
  con `Permission denied` hasta agotar el plazo. Desde una sesión SSH el mismo comando funciona,
  porque un shell de login sí lee el fichero.
  ⚠ **Por eso no se reproducía**: el 2026-09-11 por la mañana se probó «desde el mini» con
  `ssh … bash -lc`, que es justo el único entorno que NO falla. Para probar el camino del bot hay
  que usar el buzón `data/entrada/`, no SSH.
  Desde entonces `provision` llama a `reiniciar_servicios()` al final, después de
  `hacer_lanzador()`. **No lo quites**: sin eso, cualquier variable nueva que se escriba tarde
  nace invisible.
  ⚠ **Y ese arreglo NO alcanza a las máquinas que ya estaban vivas, que es donde volvió a
  morder el 2026-09-11 por la tarde.** Un `launch mini` desde el bot de un **dev** murió con
  `root@161.35.50.148: Permission denied (publickey)` intentando
  `/home/deploy/.ssh/do_droplet`: el mismo hueco por la otra punta. El droplet estaba
  **perfecto** —comprobado entrando en él con `~/.ssh/do_flota` desde la laptop: cloud-init
  `done`, `DEV_READY` puesto—, sólo que a medio hacer, porque `launch` muere antes de
  `provision`. **Lo que no valía era la clave elegida en el lado que lanza.**
  Por eso la elección ya no depende del entorno, y va en **dos** capas porque una no llegaba:
  - `fichero_clave_ssh()` usa la de `DO_SSH_KEY_FILE` **si existe** y, si no existe, **cae a
    la clave de la flota**. Es local e instantánea, que es lo que tiene que ser: por ahí pasa
    todo el `ssh` del lanzador.
  - `launch` comprueba **antes de crear** que esa clave esté entre las que el droplet llevará
    (`comprobar_clave_de_entrada()`, antes incluso de preguntarle a GitHub). Y si no lo está
    pero la de la flota **sí está registrada**, se cambia a ella en vez de morir: no se muere
    teniendo delante una clave que sí entra.
  ⚠ **Y la segunda capa es la que resolvió el caso, no la primera**, que es justo lo que uno no
  adivina: en el dev, `~/.ssh/do_droplet` **existía** y **nunca estuvo registrado en la
  cuenta**. O sea que «si no existe, cae a la flota» no le servía de nada: el fichero estaba, y
  era el equivocado. **Un fichero que existe y no autentica es tan inservible como uno que
  falta**, y mirando el disco no se distinguen; se distinguen preguntándole a la cuenta.
  ⚠⚠ **Y de dónde salía ese fichero, que es lo peor de todo: lo creaba el `post` del tipo.**
  `VAST_SSH_KEY_FILE` tenía por defecto **la misma ruta** que `DO_SSH_KEY_FILE`
  (`~/.ssh/do_droplet`), y los `post` de `types/dev.json` y `types/mini.json` corren
  `vast_instance.py register-key`, que genera ese par y lo registra **en Vast.ai y no en
  DigitalOcean**. Visto en vivo al recrear el mini el 2026-09-11: «No hay clave en
  /home/deploy/.ssh/do_droplet; generando un par ed25519 … Clave registrada en Vast.ai»,
  comentario `mini`. O sea que **toda máquina de la flota nacía con un fichero que existe y no
  sirve para entrar en un droplet**: el caso no era una casualidad de un dev, era el estado
  normal.
  **Arreglado el 2026-09-11: `VAST_SSH_KEY_FILE` es `~/.ssh/vast`**, una ruta por proveedor.
  Los dos scripts no comparten código a propósito, y tampoco deben compartir ficheros.
  ⚠ **Y no era un literal heredado, era una decisión escrita**: el `.env.example` lo explicaba
  como «por defecto la misma que la de DigitalOcean», y **cuando se escribió era razonable**,
  porque los droplets se entraban con esa clave. Lo que la volvió una trampa fue la **clave de
  flota**, que movió DigitalOcean a `~/.ssh/do_flota` y dejó la vieja ruta ocupada por un
  fichero inservible. Es la forma de la lección, más que el caso: **una decisión correcta se
  convierte en trampa cuando cambia lo que la hacía correcta, y nada avisa** — porque el
  comentario que la justificaba sigue ahí, leyéndose como si aún valiera.
  Lo protege `tests/test_clave_vast.py` (3 tests), que mira los dos defectos **y** el
  `.env.example`: un `.env` con la ruta vieja pisa el defecto y el arreglo deja de existir en
  esa máquina.
  ⚠ Al migrar, en una máquina de la flota `~/.ssh/do_droplet` es **sólo** la clave de Vast (la
  de DigitalOcean es `do_flota`), así que ahí se **renombra** y la clave sigue registrada en
  Vast sin tocar nada. **En la laptop no**: ahí `do_droplet` es la clave de DigitalOcean de
  verdad, y moverla deja la máquina sin acceso.
  Que la de flota valga no es adivinar: está registrada por definición y `DO_SSH_KEYS` vacío
  mete todas las de la cuenta en cada droplet nuevo, así que es la clave que el droplet de
  enfrente acepta seguro.
  Lo demás, por lo de siempre: el aviso sale **una vez** y no en cada sonda; **cuando no se
  puede saber —falta la `.pub`— avisa y sigue**, porque no saber no es saber que va mal.
  13 tests en `tests/test_clave_de_entrada.py`, uno de ellos sobre el **orden** dentro de
  `cmd_launch`: el freno no puede acabar por debajo del acelerador.
- **Una clave RECHAZADA no se arregla esperando, y ahora no se espera.** Un droplet sólo acepta
  las claves registradas **cuando se creó**, así que un `Permission denied` es definitivo. Tras
  `RECHAZOS_FATALES` (6 sondas, ~1 min) la espera muere diciendo **con qué clave** se estaba
  intentando y que mire `keys`. Un `Connection refused` NO mata: durante el arranque sshd se
  reinicia y eso sí se arregla solo.
- 21 tests en `tests/test_espera_arranque.py`: `python3 tests/test_espera_arranque.py`.

### Y lo que hacía largo el arranque: `package_upgrade`

Desglose medido el 2026-09-11 en un dev real (293 s del arranque a `DEV_READY`): **119 s el
`dist-upgrade` de 148 paquetes**, 57 s el resto del módulo de apt, 45 s los scripts de
DigitalOcean, **40 s el `runcmd` entero —con Node, Claude Code, gh y uv dentro—** y 16 s los
`packages:` de la plantilla. O sea que **lo que justifica la máquina costaba 40 s**, y el 40 %
del arranque se iba en poner al día un sistema que va a vivir horas.

Y el problema no era el 40 %: **ese número crece solo**, cuantos más meses pasen desde que
DigitalOcean refrescó la imagen. Es la clase de coste que un día se sale del plazo.

Desde entonces las dos plantillas llevan `package_upgrade: false` (y `package_update: true`, que
hace falta para que los `packages:` no pidan versiones que ya no están). Medido después, mismo
día y misma región: **168 s contra 293 s**, con el módulo de paquetes de 192 s a 68,8 s.

- **Lo que se pierde y por qué se puede perder:** la máquina nace con la imagen tal como la
  publica DigitalOcean. `unattended-upgrades` viene armado y `enabled` (comprobado:
  `APT::Periodic::Unattended-Upgrade "1"`), así que los parches entran solos después, en segundo
  plano, sin bloquear el arranque.
- **Y hay un motivo que no es de tiempo:** de esos 148 paquetes, 6 eran de `openssh` o del
  kernel. Actualizar `openssh-server` en pleno primer arranque es justo lo que este fichero tiene
  anotado como causa de que sshd se quede sin arrancar, y el kernel nuevo no hace nada sin
  reiniciar —y aquí no se reinicia—.
- ⚠ **`packages:` NO se toca: son 16 s.** El instinto decía «quita `build-essential`»; la
  medición dice que no hay nada que ganar ahí. Mídelo antes de recortar.

⚠ Y lo que NO se sabe, dicho como lo que es: **no se ha podido reproducir el fallo de aquella
noche.** El 2026-09-11 se lanzó un `dev` desde la laptop y un droplet desde el mini, y los dos
nacieron bien. O sea que esto no es «arreglado el fallo», es «la próxima vez el fallo dirá qué
le pasa» más «la causa más probable, que era una carrera, ya no puede darse».

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
- **Pero `update` NO instala un servicio que todavía no está** — y ese hueco costó
  entenderlo. Trae el código y reinicia lo que ya existe; una unidad **nueva** sólo la
  escribía `provision`, desde la máquina lanzadora. Así que declarar un servicio en
  `types/dev.json` llegaba a los droplets **futuros** y no al que lo declaraba, cuya única
  salida era rehacer una máquina perfectamente viva. De ahí `install-service`, que corre
  **dentro** de la máquina y reusa `build_service_section`, **el mismo generador que usa
  `provision`**: un segundo escritor de la misma unidad diverge del primero, y el que se
  depura después es siempre el que no escribiste tú.
  ```bash
  python3 scripts/do_droplet.py install-service --service foveal-vision-web
  ```
- **`dev` lleva DOS servicios desde el 2026-08-29**, y pueden convivir porque son repos
  distintos: `telegram-coordinator` y `foveal-vision-web` (la web app de `foveal-vision`,
  API + UI en `:8010`). El límite de `selected_services()` es el **directorio**, no la
  cantidad. Lo que el lanzador sabe de esa app es lo mínimo —repo, cómo se instala, cómo se
  arranca—: el **cómo** vive entero en `foveal-vision/scripts/web_app.py`, que es de quien
  la produce y no de quien la transporta (R7).
  ⚠ **El puerto lo abre el `install` del servicio en `ufw`, no `cloud-init`**: cloud-init
  vale para **todas** las máquinas y ese puerto sólo tiene sentido donde corre ese servicio.
  Y ⚠ **el API de esa app borra datos sin preguntar**, así que se niega a arrancar expuesto
  sin token; el token puede viajar desde aquí con `FVW_WEB_TOKEN` (`env_prefix: "FVW_"`),
  que es la única forma de que **sobreviva** a rehacer el dev.
- **Un servicio que se sirve por HTTP declara `url`, y `launch` lo anuncia al terminar.**
  Hasta el 2026-08-30 la máquina nacía con la app servida y el puerto abierto en `ufw`, y
  **el resumen del lanzamiento no la mencionaba**: había que entrar a preguntar por una
  dirección que la propia máquina ya sabía. La dirección no se puede escribir en el
  descriptor —lleva la IP, que nace con el droplet, y el token, que se genera dentro—, así
  que el descriptor declara el **comando** (`"url": "python3 scripts/web_app.py url"`) y el
  lanzador lo ejecuta dentro, como el usuario de desarrollo. Mismo reparto que `install` y
  `start`: el **cómo** es de quien produce, no de quien transporta (R7).
  ⚠ **Como root NO funciona**, y ése es el detalle del que depende todo: el token vive en
  `~/.config` del usuario de desarrollo, así que con root `~` es `/root` y el comando
  contesta «no hay token» en una máquina donde sí lo hay (medido el 2026-08-30 en un dev
  con `foveal-vision-web`: como `deploy`, la URL y exit 0; como root, exit 1).
  ⚠ **Y esa dirección lleva el token dentro, o sea que ES la llave**: se imprime en la
  terminal y, si lanzaste desde Telegram, queda en el chat. El freno es cerrar el puerto
  (`web_app.py cerrar`, o el ejecutor `fvweb`), que no mata el proceso.
  Ocho tests en `tests/test_url_servicio.py` —los primeros del repo—: `python3
  tests/test_url_servicio.py`.
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

- **Para PROBAR el camino de Telegram no hace falta Telegram: `data/entrada/`.** Es el
  camino de depuración que faltaba, y resuelve un problema real: un mismo comando por
  SSH y por el bot **no corre en el mismo entorno**, y esa diferencia ya mordió dos veces
  (`DO_TOKEN`, y el de Vast el 2026-08-20). Hasta ahora, ejercitar el entorno del bot
  exigía un humano con el móvil; con esto se hace desde cualquier sitio con SSH.
  El coordinador vigila `~/src/telegram-coordinator/data/entrada/*.json` (watcher +
  sondeo de 2 s) y lo mete por **`processIncoming`**, la misma función que atiende un
  mensaje de Telegram — o sea que hereda el `process.env` del bot, que es justo lo que se
  quiere probar. El fichero:

  ```json
  { "sesion": "<chatId>_main", "texto": "list", "cuando": "<ISO-8601 UTC>" }
  ```

  Escríbelo como `.tmp` y `mv` a `.json`: el watcher dispara con el primer byte y un JSON
  a medias se aparta como roto. Medido el 2026-09-10 contra el mini: `list` inyectado
  así salió por el chat con su eco y su respuesta.

  Cuatro cosas que hay que saber antes de usarlo, y ninguna es opcional:
  - ⚠ **Necesita una sesión ABIERTA, y eso sólo lo hace un humano** con `/use <ejecutor>`
    desde Telegram. Sin ella `atenderUno` no ejecuta nada y contesta que la abras.
  - ⚠ **La sesión decide QUÉ corre tu texto.** Con `/use lanzar` va al ejecutor; con
    `/use c` se lo come `claude` con `bypassPermissions`. No es lo mismo, y el fichero
    no lo elige: lo eligió quien abrió la sesión.
  - ⚠ **Se hace ECO en el chat del dueño**, marcado `📱 (desde la app)`. No es silencioso:
    lo que inyectes lo ve el usuario. Y sale como mensaje del BOT a propósito — la Bot API
    no deja publicar en nombre de una persona, y fingir que eres él sería mentir sobre
    quién escribió.
  - **Caduca a los 15 min** (`COORD_ENTRADA_TTL_MS`). Si el bot estaba parado, lo viejo
    se aparta y se avisa en vez de ejecutarse: una orden de hace horas con
    `bypassPermissions` puede alquilar máquinas que ya no quieres.

  El mecanismo es del coordinador y vive allí ([`src/entrada.ts`](https://github.com/stalinbeltran/telegram-coordinator/blob/main/src/entrada.ts),
  `c824194`); aquí sólo se apunta que existe y cómo usarlo desde este repo.
  ⚠ **El journal no sirve para leer la respuesta entera**: `[OUT]` se corta a longitud
  fija. Para ver el resultado completo hay que mirar el chat.

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
- **El de GitHub tiene TRES destinos, y el tercero no es una variable de entorno.**
  Además de `dev-secrets.env` (donde van `GITHUB_TOKEN` y `GH_TOKEN`) está
  `~/.git-credentials`, que es de donde saca el token **git** al hacer `pull` y `push`,
  y la sesión de `gh`. Rotarlo con `push-secret` deja el entorno al día y a git con el
  viejo: todo *parece* correcto y el `actualizar` del bot falla por autenticación en una
  máquina donde el token nuevo sí está. Por eso existe `push-github-token`, que escribe
  en los tres y conserva el resto de secretos del destino. Enviado a `mini` y `dev` el
  2026-09-04 tras rotar el PAT; comprobado con `git fetch` y `gh api user` en las dos.
- **Un token no se comprueba mirándolo: se le pregunta a su API.** El 2026-09-06 el
  `GITHUB_TOKEN` del **mini** estaba revocado y todo *parecía* correcto: formato bueno, 93
  caracteres, y `provision` copiándolo a sus tres destinos sin una queja. Lo que se rompió
  fue el `git clone` de **`foveal-vision-data`, que es PRIVADO** (comprobado el 2026-09-06
  contra la API: 200 con token, 404 anónimo) — o sea que el dev nació **sin el sitio donde
  se guarda lo medido**, que es exactamente la avería del 2026-08-27 entrando por otra
  puerta. Y `provision` lo dijo con un `AVISO` entre cien líneas y **salió con 0**, así que
  el lanzamiento dio el trabajo por bueno. Desde entonces:
  - `launch` y `provision` preguntan a `https://api.github.com/user` **antes** de crear ni
    tocar nada, y mueren si contesta 401. `--sin-github` es la salida de emergencia (nace
    sin los repos privados) para cuando hace falta la máquina y no hay token que valga.
  - **No saber no es saber que va bien, ni al revés**: un 403 (rate limit) o una red caída
    avisan y siguen. Bloquear por no poder preguntar dejaría sin lanzar desde el móvil.
  - Un repo que no se clona hace que el script salga con `PROVISION_INCOMPLETO` (3) y lo
    diga **por stderr**, que es lo único que el coordinador publica en el chat cuando el
    código no es 0. `launch` imprime su resumen entero —IP, cómo entrar, cómo destruirla— y
    **después** muere: la máquina ya existe y factura.
  ⚠ **Y la pista engaña.** `credential.helper store` borra la credencial en cuanto GitHub la
  rechaza una vez, así que `~/.git-credentials` queda en **0 bytes**, que se lee como «nunca
  llegó el token» — lo contrario de lo que pasó. No diagnostiques por ahí: pregúntale a
  GitHub.
- **`--push-env` lee el entorno de la máquina QUE LANZA, no el tuyo.** Desde el mini, eso
  es el `.env` del bot con el prefijo `TGL_` quitado. Si falta la variable, `launch` crea
  el droplet igual y sólo avisa: nace sin poder alquilar y se descubre tarde, ya dentro.
  Por eso `TGL_VAST_AI_API_TOKEN` tiene que estar en el `.env` de la laptop y haberse
  enviado al mini.
- **Las máquinas de larga vida no llevan el tag de los efímeros.** El mini se crea con
  `--tag control` justamente para que `destroy --tag ephemeral --yes` no se lo lleve.
- **NUNCA destruyas el droplet `mini` en una limpieza**, y desde el 2026-09-10 el motivo
  es OTRO. Ya no es que sea la única con privilegios —son iguales— ni que rehacerla exija
  la laptop —un dev la rehace en 5 minutos, probado—. Es que es **la única siempre
  encendida**, y que rehacerla **le cambia la IP**: lo que apuntara a la vieja deja de
  resolver, y si el usuario está fuera de casa se queda sin mando mientras tanto.
  "Borra todos los droplets", "limpia lo que quede" o cualquier barrido significan **las
  máquinas de trabajo**, nunca la de control. El mini sólo se destruye si el usuario lo
  pide **por su nombre y a propósito**. Si te lo encuentras en una lista que ibas a
  barrer, exclúyelo y dilo; si crees que hay que tocarlo, pregunta antes.
  ⚠ **Y no cuentes con que algo te pare**: `cmd_destroy` no comprueba el tag `control` ni
  ningún otro. Comprobado el 2026-09-10 destruyendo el mini desde un dev.

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

## Pendientes abiertos

Lo que está decidido que hay que hacer y todavía no se ha hecho. **No lo hagas por tu
cuenta**: cada uno dice de quién es. Cuando se cierre, se borra de aquí y lo que se
aprendió se anota donde corresponda.

- **⏳ DECIDIR: `ejecutar_post` corre DESPUÉS de `reiniciar_servicios`, y el último
  fichero que se escribe queda sin releer.** Anotado el **2026-09-11**, al arreglar el
  `AttributeError` de `reiniciar_servicios` (ver su docstring). El orden es
  `hacer_lanzador` → `reiniciar_servicios` (`do_droplet.py:2949`) → vuelve →
  `ejecutar_post` (`:1211`). Y uno de los `post` de `types/dev.json` **y** de
  `types/mini.json` es `entornos aplicar`, que **reescribe el `.env` del bot** y
  **no reinicia nada** (`_entornos_aplicar` no tiene ningún `systemctl`). El bot lee
  su `.env` al arrancar.
  ⚠ **Sospecha, NO comprobada:** hoy puede que no dé síntoma, porque los valores que
  escribe `entornos aplicar` salen de las mismas `TG_*`/`TGL_*` que ya tenía. Pero es
  **la misma clase de fallo** que el docstring de `reiniciar_servicios` dice cerrar —
  «el entorno de un proceso es una foto de cuando arrancó»— y aquí es el `.env`, no el
  entorno. Lo que hace falta es **medirlo**, no razonarlo.
  **Qué hay que decidir** (y no improvisar): si el reinicio se mueve a después de los
  `post`, o si hay **dos** reinicios. Mover no es gratis: `hacer_lanzador` escribe la
  clave antes, así que un único reinicio al final tiene que seguir cubriendo ese caso.
  Va con su prueba, y la prueba tiene que fallar con el orden de hoy (R17).

- **⏳ DEL USUARIO: sacar los DATOS de un repo de git a un volumen (o a Spaces) de
  DigitalOcean.** Anotado el **2026-09-11**, pedido por el dueño con estas palabras: *«un
  repo no es buen lugar»*. Es la condición para poder quitar `foveal-vision-data` del mini,
  que hoy es lo único de trabajo que clona.
  **El dato que lo motiva** (medido el 2026-09-11 con `du -sh`, idéntico en mini y dev):
  `foveal-vision-data` pesa **628 MB**, de los que **337 MB son `.git/objects/pack`**. Git
  guarda **todas** las versiones para siempre, y el repo recibe un commit
  `conversaciones: archivo automático` por sesión más los runs de cada experimento. O sea
  que crece monótonamente y **no se puede podar**. Hoy el mini arrastra esos 628 MB para
  escribir un log de errores de 8 líneas (`telegram-coordinator/scripts/errores.mjs:51-56`).
  **El mecanismo ya existe y es dato, no código**: `volume create|list|attach|detach|destroy`
  (`cmd_volume`, [do_droplet.py:1570](scripts/do_droplet.py#L1570)) y un **tipo puede
  declarar `"volume": "<nombre>"`** ([do_droplet.py:1088](scripts/do_droplet.py#L1088)), que
  se comprueba **antes** de crear el droplet para que un nombre mal escrito salga gratis.
  Cuesta **0,10 $/GB y mes** y **sobrevive a su droplet**, que es justo la propiedad que
  falta. Hoy **ningún tipo lo usa**.
  ⚠⚠ **Pero un volumen NO resuelve esto tal cual, y ésa es la parte que hay que decidir
  antes de tocar nada.** Dos límites, los dos ya documentados y los dos mordientes:
  1. **Un volumen se conecta a UNA máquina a la vez, y en su misma región.** Mini y dev no
     pueden montar el mismo. Si el bot de las **dos** tiene que escribir sus errores, un
     volumen no es la respuesta para *ese* trozo.
  2. **No se puede conectar un volumen de DO a una máquina de otro proveedor**
     (`scripts/dataset.py`, cabecera). Los estudios corren en **Vast**, así que el dato que
     ellos producen o consumen no puede vivir sólo ahí.
  **Por eso la pregunta no es «¿volumen sí o no?» sino «¿qué dato es cada cosa?».** Hoy
  `foveal-vision-data` mezcla al menos tres, con necesidades opuestas:
  | qué | tamaño | quién escribe | qué querría |
  |---|---:|---|---|
  | `errores/` | KB | el bot de **cada** máquina, siempre | algo compartido y de sólo-añadir; un volumen **no** vale (límite 1) |
  | `conversaciones/` | 30 MB y subiendo | el hook, una vez por sesión | almacenamiento de objetos (**Spaces**): se escribe una vez y casi no se lee |
  | `runs/`, `sweeps/`, `2026/`, `preprocesado/` | ~350 MB | los estudios, en Vast | **el candidato real** a volumen o Spaces |
  ⚠ **Spaces (S3) cubre los tres límites** —se llega desde cualquier proveedor, no hay
  conexión exclusiva— y `dataset.py` **ya sabe leer de una `url`**, así que ese camino no
  pide mecanismo nuevo. No está medido ni presupuestado: es lo primero que habría que mirar.
  ⚠ **Y lo que NO hay que hacer sin decidir lo de arriba**: quitar `foveal-vision-data` del
  mini. Ahí `errores.mjs` devuelve `null` **en silencio** y la única máquina siempre
  encendida se queda sin log de errores — y su `journalctl` muere con ella.

- **⏳ DEL USUARIO: repasar la lista de ejecutores y quitar los que no usa.** Anotado el
  2026-09-10. Hay **40 ejecutores** cargados en una máquina de la flota (medido ese día en
  el journal del mini, de tres fuentes: `telegram-coordinator`, este repo y
  `foveal-vision`), de los cuales **11 los declara este repo** (`telegram/executors/`).
  El usuario dice que varios no los usa nunca y probablemente sobran.
  Por qué importa y no es limpieza cosmética: la lista es lo que el bot enseña con
  `/executors`, y es de donde se elige con `/use` — **y la sesión abierta decide qué corre
  el texto que llegue**, incluido el que entra por `data/entrada/` sin pasar por Telegram.
  Una lista larga de cosas que nadie usa hace más fácil abrir la sesión equivocada.
  ⚠ **La decisión de cuáles sobran es SUYA, no tuya**: desde fuera, un ejecutor que no se
  ha usado en meses y uno que es el freno de una emergencia se parecen mucho (`apagar-do`
  y `apagar-vast` son exactamente eso). Si te lo pide, lo que sí puedes aportar es el
  dato: cuáles existen, qué repo declara cada uno y cuáles aparecen en el log de mensajes.
- **✅ CERRADO el 2026-09-11: la `CWEB_TS_AUTHKEY` SÍ es Ephemeral.** Se deja escrito porque
  el pendiente que había aquí **afirmaba lo contrario**, y al vivir en este fichero se
  recargaba en cada sesión y se le repetía al usuario como un hecho.
  **`nodo.mjs` nunca afirmó eso.** Lo que emite es un aviso de **deriva de nombre** («este
  nodo no se llama `dev`, se llama `dev-1`») y, al final, una nota **condicional** que dice
  literalmente *no toques la authkey* salvo que un nodo **recién caído** siga registrado al
  día siguiente. Ese condicional se resumió aquí como afirmación plana.
  La causa real de los `dev-1`/`dev-2` ya estaba identificada en el propio pendiente: quien
  destruyó corría `ab18d1a`, anterior a `8e3efc6`, así que **el `tailscale logout` nunca
  llegó a ejecutarse**. Comprobado el 2026-09-11 con `pre_destroy` ya en las dos máquinas:
  tras destruir y rehacer el dev, la tailnet tenía sólo `dev` (online) y el móvil, sin un
  solo nodo muerto, y el nuevo cogió el nombre `dev`.
  Lo que se queda como regla, porque vale para cualquier repo: **un aviso condicional no se
  resume como afirmación.** «Sólo si X, entonces Y» escrito como «Y» es un hecho falso que
  además se reinyecta en cada sesión. Se copia con su condición o no se copia.
  Y lo que sigue siendo verdad y no hay que perder: **`pre_destroy` corre en la máquina que
  DESTRUYE**, así que un destructor con el repo viejo no recoge nada y no lo dice.
