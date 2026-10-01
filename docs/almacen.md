# El ALMACÉN: el volumen `datos` del mini como remoto git de los datos

**2026-10-01.** Pedido por el dueño, literal:

> «Revisa si tenemos forma de contratar un volumen de 1GB en digital ocean, y si se puede
> conéctalo al mini y usémoslo para ahí guardar data que deba mantenerse en disco, de modo
> que todos los servers puedan accesarla. […] Nadie debe poder borrar nada (solo nosotros).
> Luego modifica las instrucciones para que todo dato, aun siendo temporal se guarde ahí en
> vez de en el repo de datos como hemos hecho hasta ahora […]. Si conviene puedes usar un
> git local en el mini para guardar todo como si fuese un repo github en vez de crear un
> webservice (me suena mas limpio). Este volumen debe sobrevivir al cierre del mini, así que
> luego de probar que todo funcione destruye el mini, lo vuelves a crear, y el volumen debe
> volver a enlazarse al mini (el codigo para montarlo debe estar ya en el mini, para que
> automáticamente se enlace)»

## 1. Qué es, en una frase

Un volumen de bloques de DigitalOcean (`datos`, 1 GB, `nyc1`, **0,10 $/mes**) conectado al
mini y montado en `/mnt/datos`, que guarda **repos git desnudos** servidos por SSH desde un
usuario sin shell. `foveal-vision-data` —el repo de datos de siempre— vive ahí como origen;
cada máquina de la flota apunta su `origin` al mini, y GitHub queda como copia congelada.
Git y no un servicio web, porque el dueño lo pidió así y porque **git ya trae la regla
«nadie borra»**: con `receive.denyDeletes` + `receive.denyNonFastForwards` + un hook, no se
borran ramas ni se reescribe historia, y un fichero borrado en un commit sigue ahí.

## 2. Cómo está montado

| pieza | dónde | qué hace |
|---|---|---|
| el volumen | DigitalOcean, `nyc1`, 1 GB | de la cuenta, no del droplet: **sobrevive a destruir el mini** |
| `types/mini.json` → `"volume": "datos"` | lanzador | `launch mini` lo conecta al crear el droplet y lo monta en `/mnt/datos` con `nofail` en fstab |
| `types/mini.json` → `"almacen"` | lanzador | DATO: qué repos se espejan (`repos`) y qué carpetas de apps pasan al volumen (`apps`) |
| `do_droplet.py almacen instalar` | corre EN el mini (su `post`) | usuario `datos` con `git-shell`, clave de flota con `restrict`, repos desnudos en `/mnt/datos/git/`, config + hook «nadie borra», carpetas de apps enlazadas. **Idempotente**: en un mini rehecho no vuelve a espejar nada |
| `do_droplet.py almacen conectar` | en CADA máquina (su `post`, y el ejecutor) | resuelve la IP del mini por la API, escribe el alias `almacen` en `~/.ssh/config` y apunta `origin` de `~/src/foveal-vision-data` a `almacen:/mnt/datos/git/foveal-vision-data.git` |
| `do_droplet.py almacen estado` | cualquiera | montaje, fstab, usuario, repos con su regla, pushes registrados, apps, y si el volumen está conectado |
| `do_droplet.py almacen probar` | cualquiera | la prueba de aceptación: un push entra, borrar se rechaza, forzar se rechaza, y sólo root limpia |
| `volume resize datos --size-gb N` | cualquiera | crece el volumen (DO no encoge) y hace `resize2fs` en caliente |
| ejecutor `almacen` | Telegram (bots de dev y mini) | `estado` · `conectar` · `probar` · `instalar --seco` |

**«Nadie borra» quiere decir esto, y no más:** por el camino normal —`git push` desde
cualquier máquina de la flota— no se puede borrar una rama ni reescribir historia, y el
usuario `datos` no puede ejecutar nada que no sea git. **«Nosotros»** es root en el mini (la
clave de flota, `sudo`), que es quien puede tocar `/mnt/datos` a mano; y quien tenga
`DO_TOKEN` puede `volume destroy`. No protege contra un administrador: protege contra el
borrado **rutinario y accidental**, que es el que pasa.

## 3. Lo que sobrevive a qué (con complemento, como manda la regla de escritura)

| | reiniciar el mini | destruir y rehacer el mini | destruir el VOLUMEN |
|---|---|---|---|
| los repos del almacén | ✅ fstab | ✅ el volumen es de la cuenta; `launch mini` lo reconecta | ❌ |
| la carpeta `datos/` de sispla-demo | ✅ | ✅ es un enlace al volumen | ❌ |
| la IP del mini | ✅ | ❌ cambia → cada dev repite `almacen conectar` | — |
| la clave de host SSH del mini | ✅ | ❌ cambia → `conectar` olvida la vieja y acepta la nueva (y lo dice) | — |
| GitHub (`github`) | copia congelada: no se actualiza | | |

⚠ **El volumen es UNA copia.** DigitalOcean lo replica dentro de su centro de datos, pero
no contra `volume destroy` ni contra un error en la cuenta. Un espejo periódico a GitHub
(`git push --mirror github` desde el mini) costaría cero y lo cubriría; **no está
puesto** porque el dueño pidió «en vez de» GitHub. Es una decisión suya, no un olvido.

## 4. Lo que la revisión previa encontró y cómo quedó

| hallazgo del `revisor` | qué se hizo |
|---|---|
| datos sólo en el disco del mini (`errores/` sin empujar, `sispla-demo/datos/`) | se empujan antes; `sispla-demo/datos/` pasa al volumen con un enlace (`apps` del tipo) |
| un `origin` con la IP del mini se rompe al rehacerlo | alias `almacen` en `~/.ssh/config` con `HostKeyAlias`; la IP la da la API en cada `conectar` |
| con `volume` en el tipo, `launch mini2` (staging, flota-simetrica §5) muere | `launch --sin-volumen`; el almacén se le conecta después |
| si el montaje falla, todo sigue en verde | `instalar` se NIEGA si `/mnt/datos` no es un punto de montaje (test); y sin montaje no existe `/mnt/datos/git`, así que los push fallan ruidosamente |
| «todo dato» sin borrar no cabe en 1 GB | `volume resize`; `estado` enseña el uso; la regla de qué entra está en el CLAUDE.md del coordinador |
| el dev nuevo clona de GitHub (dato viejo) | su `post` hace `almacen conectar`, que pone `origin` en el mini y avanza `main` en fast-forward |
| 512 MB para servir clones de ~350 MB | `pack.threads=1`, `pack.windowMemory=32m`, `core.packedGitLimit=64m`… en cada repo; medido abajo |

## 5. La sesión, de principio a fin (ejecutada el 2026-10-01)

*Todo lo de abajo está pegado de la salida real, salvo donde se marca.*

```
dev → python3 scripts/do_droplet.py volume create datos --size-gb 1 --region nyc1
    ← Creando volumen 'datos': 1 GB en nyc1, ext4.
      Coste: $0.10/mes mientras exista, esté conectado o no.
      Creado (id d5527d33-…).

dev → python3 scripts/do_droplet.py volume attach datos --droplet mini
    ← Conectando 'datos' a 'mini'…
        volumen ya formateado (ext4), no se toca
        montado en /mnt/datos (801M libres)

dev → python3 scripts/do_droplet.py remoto mini almacen instalar
    ← Instalando el almacén en /mnt/datos (como root, vía sudo):
        usuario datos creado (git-shell)
        espejando foveal-vision-data desde GitHub (una vez en la vida)...
        foveal-vision-data.git listo: 761 commits, 355M
        sispla-demo: lo que habia en /home/deploy/src/sispla-demo/datos pasa al volumen
        sispla-demo: /home/deploy/src/sispla-demo/datos -> /mnt/datos/apps/sispla-demo

dev → python3 scripts/do_droplet.py remoto mini almacen conectar
    ← Estoy en el mini: el alias `almacen` apunta a 127.0.0.1.
        foveal-vision-data: origin -> almacen:/mnt/datos/git/foveal-vision-data.git
        foveal-vision-data: al día con el almacén

dev → python3 scripts/do_droplet.py almacen conectar          (en el propio dev)
    ← El almacén está en 'mini' (157.230.221.59).
        foveal-vision-data: GitHub se queda como remoto `github` (copia congelada)
        foveal-vision-data: origin -> almacen:/mnt/datos/git/foveal-vision-data.git
        foveal-vision-data: al día con el almacén

dev → python3 scripts/do_droplet.py almacen estado
    ← volumen   datos: 1 GB en nyc1, conectado al droplet 600796768
      montado   /mnt/datos
      disco     356M usados de 868M (45%)
      fstab     si (nofail)
      usuario   datos, shell /usr/bin/git-shell
      repo      foveal-vision-data.git  355M  761 commits  ultimo 2026-10-01T13:08:21+00:00
                nadie borra: si (denyDeletes + hook)
      pushes    0 registrados (todavia ninguno)
      app       sispla-demo: 312K

dev → python3 scripts/do_droplet.py almacen probar
    ←   ok    el almacén contesta (ls-remote)
        ok    un push normal entra
        ok    borrar una rama se RECHAZA
        ok    reescribir historia se RECHAZA
        ok    root en el mini SÍ puede quitar la rama de prueba
      El almacén cumple: entra lo nuevo, no se borra nada, y sólo root limpia.
```

**Dos medidas que la revisión pedía antes de cambiar ningún `origin`:**

| | medido 2026-10-01 |
|---|---|
| clon completo desde el mini al dev (`git clone almacen:…`) | **21,05 s**, 356 MB, 362 MB de RSS en el dev |
| el mini durante ese clon (512 MB) | **0** eventos OOM en `dmesg`; 296 MB disponibles después |
| el primer fallo real al instalar | `estado` decía «? commits» y «nadie borra: NO» con la regla puesta: `git` como root se niega a leer un repo de otro dueño («dubious ownership»). Arreglado en `f7b8c51`: las consultas van como `datos` |

**Antes de tocar el `origin` del mini se rescató lo que sólo existía en su disco**, que es
el hallazgo 1 de la revisión: una línea de `errores/` sin empujar (`15e683b`, a GitHub,
**antes** de espejar, para que el espejo naciera con ella), y los 312 KB de la demo de
SisPla, que ahora viven en el volumen.

### 5.1 Destruir y rehacer el mini

*Corrido el 2026-10-01 desde el dev, de verdad. Salidas pegadas; los tiempos son de reloj.*

```
dev → volume detach datos
    ← Desmontando en 'mini'… Desconectando 'datos' del droplet 600796768… Desconectado.
dev → destroy mini --yes
    ← Destruido mini.                       (el volumen queda suelto: `volume list` sin dueño)
dev → launch mini                           13:14:09 → 13:18:48 UTC  (4 min 39 s)
    ←   Lanzador: al día con origin/main.
        Volumen: 'datos' (1 GB) se montará en /mnt/datos
        Activo. IP pública: 142.93.255.224   (la IP cambió, como estaba escrito)
        …provision: telegram-launcher, sispla-demo, gauss-p, graph-simulator…
        Montando el volumen 'datos'…
          volumen ya formateado (ext4), no se toca
          montado en /mnt/datos (446M libres)   ← el dato estaba
        Pasos finales del tipo (4):
          almacen instalar:  usuario datos creado (git-shell)
                             foveal-vision-data.git ya existe en el volumen: no se toca su contenido
                             fatal: not in a git directory                  ← ❌ ver abajo
          almacen conectar:  foveal-vision-data: al día con el almacén      ← el servicio git SÍ servía
```

**El fallo que destapó el ciclo, y que ninguna prueba en seco podía ver:** en un mini
rehecho el repo del volumen ya pertenece a `datos`, y `git config` como root se niega
(«fatal: not in a git directory», que es «dubious ownership» con otro mensaje). El script
moría ahí, así que **la demo de SisPla no llegó a enlazarse al volumen**: el servicio git
funcionaba (el hook y la config estaban en el volumen desde la primera instalación) y el
`post` sólo dejó un `AVISO`. Arreglado en `f5ddf37` —toda orden git sobre el repo va como
`datos`— y **`estado` comprueba ahora el enlace de cada app**, porque un enlace que falta no
se veía por ningún lado.

```
dev → almacen conectar                      (en el dev, contra la IP nueva)
    ← foveal-vision-data: la clave de host del almacén cambió (mini rehecho): se olvida la vieja
      y se acepta la nueva. La IP 142.93.255.224 la acaba de dar la API de NUESTRA cuenta.
      foveal-vision-data: al día con el almacén
dev → remoto mini almacen instalar          (idempotente, ya con el arreglo)
    ← usuario datos ya existe
      foveal-vision-data.git ya existe en el volumen: no se toca su contenido
      foveal-vision-data.git listo: 762 commits, 355M
      sispla-demo: /home/deploy/src/sispla-demo/datos -> /mnt/datos/apps/sispla-demo
dev → almacen estado
    ← volumen   datos: 1 GB en nyc1, conectado al droplet 605270224   ← el droplet NUEVO
      montado   /mnt/datos · fstab si (nofail) · usuario datos, shell /usr/bin/git-shell
      repo      foveal-vision-data.git  355M  762 commits   nadie borra: si
      pushes    2 registrados
      app       sispla-demo: /home/deploy/src/sispla-demo/datos -> volumen (312K)
dev → almacen probar
    ← ok ×5. El almacén cumple: entra lo nuevo, no se borra nada, y sólo root limpia.
dev → flota
    ← Paridad correcta en 2 maquina(s).
```

| | antes de destruir | en el mini rehecho |
|---|---|---|
| commits en el almacén | 761 (+1 del README = 762) | **762** |
| `estado.json` de la demo | 152.138 bytes | **152.138 bytes** |
| volumen | conectado a 600796768 | conectado a **605270224**, montado por fstab |
| IP del mini | 157.230.221.59 | **142.93.255.224** (cambia; `conectar` lo absorbe) |
| clave de host | — | cambió; `conectar` lo dijo y la aceptó |

**Lo que este ciclo NO midió:** un **dev** nuevo naciendo con `almacen conectar` en su
`post` (está cableado en `types/dev.json`), que un dev pueda parir máquinas conectadas, y el
staging de un `mini2` con `--sin-volumen` (escrito, no corrido).

### 5.2 El dev, destruido a propósito el 2026-10-01 para medir las dos primeras

El dueño destruyó el dev ese mismo día (*«nos ha pasado que algo falta»*). El guion que el
dev nuevo tiene que correr nada más nacer está en `telegram-coordinator/CLAUDE.md` § «LO
PRIMERO SI ACABAS DE NACER», punto 0; el resultado se pega **aquí**:

PENDIENTE_DEV_NUEVO

## 6. La regla para quien escriba datos (está en `telegram-coordinator/CLAUDE.md`)

Desde el 2026-10-01, **todo dato se guarda en el almacén**: lo que ya iba al repo de datos
(datasets, runs, conversaciones, errores) sigue yendo a `~/src/foveal-vision-data`, y su
`origin` es el almacén. Lo que antes moría en `/tmp` (logs de flota, resultados a medias)
va también al repo de datos, en `temporal/<máquina>/<fecha>/`, commiteado y empujado. **Lo
que no está empujado al almacén, no existe.** Y como nadie borra, antes de meter algo
grande (>20 MB) se mira `almacen estado`: lo único que puede pasarle al disco es llenarse.
