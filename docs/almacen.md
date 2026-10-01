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
staging de un `mini2` con `--sin-volumen` (escrito, no corrido). → Las dos primeras quedaron
medidas en §5.2; el staging sigue sin correr.

### 5.2 El dev, destruido a propósito el 2026-10-01 para medir las dos primeras

El dueño destruyó el dev ese mismo día (*«nos ha pasado que algo falta»*). El guion que el
dev nuevo tiene que correr nada más nacer está en `telegram-coordinator/CLAUDE.md` § «LO
PRIMERO SI ACABAS DE NACER», punto 0; el resultado se pega **aquí**:

✅ **Las dos mitades en verde, medido el 2026-10-01 desde el dev nuevo** (droplet 605307430,
nacido hacia las 15:58 UTC). Salidas pegadas; lo recortado va con `…`. El guion de la mitad 2
y su log entero están en el almacén: `foveal-vision-data/temporal/dev/2026-10-01/`
(`f7fefc06`).

**Mitad 1 — este dev nació conectado** (16:10 UTC, a los 12 min de nacer):

```
dev → git -C ~/src/foveal-vision-data remote -v
    ← github  https://github.com/stalinbeltran/foveal-vision-data.git (fetch/push)
      origin  almacen:/mnt/datos/git/foveal-vision-data.git (fetch/push)
dev → grep -A6 "Host almacen" ~/.ssh/config
    ← Host almacen · HostName 142.93.255.224 · User datos · IdentityFile ~/.ssh/do_flota
      IdentitiesOnly yes · HostKeyAlias almacen · StrictHostKeyChecking accept-new
dev → almacen estado                                    (3,4 s)
    ← volumen   datos: 1 GB en nyc1, conectado al droplet 605270224
      montado   /mnt/datos · disco 360M usados de 868M (45%) · fstab si (nofail)
      usuario   datos, shell /usr/bin/git-shell
      repo      foveal-vision-data.git  360M  766 commits  ultimo 2026-10-01T15:42:18+00:00
                nadie borra: si (denyDeletes + hook)
      pushes    7 registrados
      app       sispla-demo: /home/deploy/src/sispla-demo/datos -> volumen (312K)
dev → almacen probar                                    (3,9 s)
    ← ok ×5 · El almacén cumple: entra lo nuevo, no se borra nada, y sólo root limpia.
```

Que el `post` corrió `conectar` —y no sólo que el alias está— lo prueba el dato: `main` del
clon está en `d69f7a3d`, el HEAD del almacén, **5 commits por delante de `github/main`**
(`15e683b`). Esos 5 sólo existen en el almacén, así que sólo pudieron llegar de ahí.
⚠ `estado` dice 766 commits y `git rev-list --count HEAD`, 762: el almacén cuenta `--all`
(con `dataTests` y `datos-fechados`). No es un fallo.

**Mitad 2 — un dev pare una máquina conectada** (16:16:52 → 16:23:28 UTC, **6 min 36 s**;
≈ **0,004 $**, *derivado* de 0,0357 $/h, no leído de la factura):

Se lanzó como **unidad** (`desacoplar-persistente.sh prueba-almacen-hijo sh <guion>`) y no
desde el turno, porque la sesión era un `claude -p` de Telegram: si moría a mitad, el
`destroy` tenía que llegar igual. El guion lanza, mira desde dentro, corre `probar` desde
dentro, destruye **sin condiciones**, lista, y sale siempre con 0 (la unidad es
`Restart=on-failure`).

```
dev → launch prueba-almacen --type dev --service ''
    ← Lanzador: al día con origin/main.
      GitHub: el token sirve (usuario stalinbeltran).
      Llavero: 12/16 variables listas para viajar.           ← pendiente 3
      Aceptado (202). Droplet id 605312358. … Activo. IP pública: 104.248.224.99
      … clonando foveal-vision · image-text-sample-generator · foveal-vision-data ·
        estudios-redes-neuronales · digital-ocean-dropplet-auto-launching
      'prueba-almacen' usa la clave de la flota: … No se registra ninguna clave nueva.
      Pasos finales del tipo (3):
        register-key --comment dev → Clave registrada en Vast.ai.      ← pendiente 4
        entornos aplicar …         → no existe …/claude-code-webapp-mobile, me lo salto
                                     no existe …/telegram-coordinator, me lo salto
        almacen conectar           → El almacén está en 'mini' (142.93.255.224).
                                     alias `almacen` escrito en /home/deploy/.ssh/config
                                     foveal-vision-data: GitHub se queda como remoto `github`
                                     foveal-vision-data: origin -> almacen:/mnt/datos/git/…
                                     foveal-vision-data: al día con el almacén
dev → ssh prueba-almacen --cmd '…'                      (dentro del hijo)
    ← origin almacen:/mnt/datos/git/foveal-vision-data.git (fetch/push) · github → GitHub
      Host almacen · HostName 142.93.255.224 · User datos · … · HostKeyAlias almacen
      HEAD local: d69f7a3d 2026-10-01 15:42:18 conversaciones: archivo automático
      telegram-coordinator: inactive · foveal-vision-web: inactive · claude-web: inactive
      almacen:/mnt/datos/git/foveal-vision-data.git
      d69f7a3d829084bf07656f3f0c19613d328c2983	HEAD       ← el guion del CLAUDE.md, rc 0
dev → ssh prueba-almacen --cmd 'almacen probar'          (la ESCRITURA, que el guion no mira)
    ← ok ×5 · El almacén cumple: entra lo nuevo, no se borra nada, y sólo root limpia.
dev → destroy prueba-almacen --yes
    ← Destruido prueba-almacen.
dev → list
    ← mini (605270224) · dev (605307430). Nada más.
```

Y después, desde el dev: `git ls-remote origin 'refs/heads/prueba-almacen-*'` sale **vacío**
(el `probar` del hijo no dejó rama), y el primer push real de este dev —el del guion y el log
de arriba— entró en **0,98 s** (`d69f7a3d..f7fefc06`).

| lo que queda medido | |
|---|---|
| un dev **nuevo** nace conectado (el `post` de `types/dev.json`) | ✅ |
| un dev **pare** una máquina conectada, con su llavero y su clave de flota, sin pasar por el mini | ✅ lee **y escribe** (`probar` desde el hijo) |
| `launch --service ''` | ✅ lo acepta, y el hijo nace con las tres unidades `inactive`: **sin bot, sin 409** |

⚠ **El alcance es «un dev pare un dev»**, no «cualquier máquina»: ver el pendiente 1.

**Lo que quedó pendiente** (nada de esto lo rompió la prueba, y nada está arreglado):

1. **`bench-control` clona `foveal-vision-data` y NO se conecta al almacén.** Su `post` no
   lleva `almacen conectar` (`dev` y `mini` sí; los demás tipos no clonan el repo de datos),
   así que una máquina de ese tipo empujaría a **GitHub**, la copia congelada, **en
   silencio**: justo lo que el almacén existe para no hacer. El arreglo es añadirle el paso y
   un test que fije el invariante «todo tipo que clone el repo de datos se conecta» (R17).
   No se ha hecho: lo decide el dueño, y depende de si `bench-control` sigue en uso.
2. **El freno no ve los droplets de DO.** Con el hijo vivo y facturando (~16:22 UTC),
   `cerrable.mjs --breve` decía **«🟢 CERRABLE — nada alquilado, nada corriendo»**: sólo mira
   Vast, y su lista `TRABAJOS` no reconoce `do_droplet.py`. Si este dev se hubiera destruido en ese
   momento, el hijo habría seguido vivo, con el llavero entero, hasta un `apagar-do`. Anotado en el
   CLAUDE.md del coordinador § «¿Se puede apagar este server?».
3. **Al llavero de este dev le faltan 4 de 16, las cuatro opcionales**:
   `TGL_CLAUDE_PERMISSION_MODE`, `FVW_WEB_TOKEN`, `TGL2_BOT_TOKEN` y `TGL2_ALLOWED_USER_IDS`.
   Ninguna toca el almacén. Las dos `TGL2_` son las del bot de staging, la salida **elegida**
   en [`flota-simetrica.md`](flota-simetrica.md) §5 para rehacer el mini con el viejo vivo:
   desde este dev, hoy, ese camino no está disponible.
4. **Cada máquina que nace deja una clave más en Vast**, que queda huérfana al destruirla:
   `register-key` genera un par **por máquina**. Es deuda ya conocida (`CLAUDE.md` de este
   repo, «El goteo de claves muertas también pasa en Vast»): se podaron el 2026-09-11, de 44,
   y hoy vuelven a ser **19**, una de ellas la del hijo. `vast_instance.py keys --prune` lo
   hace, pero hay que pasarle `--keep <id>` con las de las otras máquinas vivas, y desde aquí
   no se pueden adivinar: lo decide el dueño.
5. **`probar` deja una referencia de seguimiento huérfana en el clon desde el que corre**
   (`origin/prueba-almacen-<ts>`): empuja desde el clon real, y root borra la rama en el
   servidor pero no la referencia local. Es cosmético: `git fetch --prune` la quita.

**Cómo quedó, ese mismo 2026-10-01** (la lista de arriba se deja como se escribió):

| | |
|---|---|
| 1 | ⏳ **revisado, no arreglado**: `bench-control` está abandonado de hecho desde el 2026-08-23 (`dev` asumió alquilar y apagar en Vast) y cinco documentos lo siguen dando por vigente. Lo decide el dueño: retirarlo, o conectarlo con el test del invariante |
| 2 | ✅ el freno cuenta los droplets sueltos: `list --json` aquí, el tag `atendida` que pone `launch`, y el detalle en el CLAUDE.md del coordinador |
| 3 | ✅ **15/16 en las dos máquinas**: falta sólo `TGL2_BOT_TOKEN`, que es un bot nuevo de @BotFather y sólo puede darlo el dueño |
| 4 | ✅ la flota usa la **clave de flota** también para Vast: **de 19 claves a 1** (CLAUDE.md de este repo) |
| 5 | sin tocar: cosmético |

## 6. La regla para quien escriba datos (está en `telegram-coordinator/CLAUDE.md`)

Desde el 2026-10-01, **todo dato se guarda en el almacén**: lo que ya iba al repo de datos
(datasets, runs, conversaciones, errores) sigue yendo a `~/src/foveal-vision-data`, y su
`origin` es el almacén. Lo que antes moría en `/tmp` (logs de flota, resultados a medias)
va también al repo de datos, en `temporal/<máquina>/<fecha>/`, commiteado y empujado. **Lo
que no está empujado al almacén, no existe.** Y como nadie borra, antes de meter algo
grande (>20 MB) se mira `almacen estado`: lo único que puede pasarle al disco es llenarse.
