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

PENDIENTE_SESION

## 6. La regla para quien escriba datos (está en `telegram-coordinator/CLAUDE.md`)

Desde el 2026-10-01, **todo dato se guarda en el almacén**: lo que ya iba al repo de datos
(datasets, runs, conversaciones, errores) sigue yendo a `~/src/foveal-vision-data`, y su
`origin` es el almacén. Lo que antes moría en `/tmp` (logs de flota, resultados a medias)
va también al repo de datos, en `temporal/<máquina>/<fecha>/`, commiteado y empujado. **Lo
que no está empujado al almacén, no existe.** Y como nadie borra, antes de meter algo
grande (>20 MB) se mira `almacen estado`: lo único que puede pasarle al disco es llenarse.
