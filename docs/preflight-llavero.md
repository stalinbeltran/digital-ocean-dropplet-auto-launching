# ⏳ PENDIENTE: `launch` no comprueba el llavero, y por eso una máquina puede nacer coja

**Encontrado el 2026-09-10**, al ir a rehacer el mini desde un dev. No se llegó a
destruir nada: el fallo se vio **antes** porque se miró a mano. Esa comprobación a
mano es justamente lo que falta en el código.

## El fallo, medido

`llavero.json` declara las 17 variables que hacen falta para que una máquina
**nazca entera**. Un tipo con `"llavero": true` (hoy `mini` y `dev`) las recibe de
la máquina que lanza. Pero **nadie comprueba que la máquina que lanza las tenga**.

Comparando `llavero.json` con el entorno real del dev `dev-1` ese día:

| variable | ¿estaba? |
|---|---|
| `DO_TOKEN`, `GITHUB_TOKEN`, `VAST_AI_API_TOKEN`, `CLAUDE_CODE_OAUTH_TOKEN` | ✅ |
| `TG_BOT_TOKEN`, `TG_ALLOWED_USER_IDS`, `TG_CLAUDE_PERMISSION_MODE` | ✅ |
| `TGL_BOT_TOKEN`, `TGL_ALLOWED_USER_IDS` | ✅ |
| `GIT_USER_NAME`, `GIT_USER_EMAIL`, `CWEB_TS_AUTHKEY` | ✅ |
| **`DO_SSH_USER`** | ❌ |
| **`TGL_CLAUDE_PERMISSION_MODE`** | ❌ |
| **`FVW_WEB_TOKEN`** | ❌ |
| **`TGL2_BOT_TOKEN`**, **`TGL2_ALLOWED_USER_IDS`** | ❌ |

## Por qué `DO_SSH_USER` es la cara y no una más

**Este repo ya se quemó con ella**, y está escrito en
[`reparto-mini-dev.md`](reparto-mini-dev.md) § «Rehacer el mini»:

> `DO_SSH_USER` no viajaba y no la aportaba el tipo, así que el mini renacido caía
> al default `root`. El síntoma habría sido el de siempre — `ssh` entrando como
> root, sin `dev-secrets.env` en su home, y un «falta el token» en una máquina
> donde el token sí está.

Se metió en el llavero por eso. **Y el llavero no se comprueba**, así que la
protección quedó en el sitio equivocado: la lección se aprendió, se anotó, y el
mecanismo sigue permitiendo repetirla. `do_droplet.py:53` cae a `"root"` en
silencio.

⚠ **Y el fallo se agrava con quién puede arreglarlo.** La nota de
`types/mini.json` dice que **un dev nunca puede entrar por SSH al mini** —«su
clave se registra durante SU provisión, siempre después de que esta máquina
exista»—. O sea que un mini que nazca coja **no se puede reparar desde el dev que
lo parió**: hay que rehacerlo otra vez, o entrar desde la consola de DO.

⚠ Y `TGL2_BOT_TOKEN` faltando tiene un efecto de segundo orden: es el bot de
staging, o sea **la primera de las tres salidas** que `reparto-mini-dev.md`
recomienda para reemplazar el mini sin quedarse sin mando. Sin él, sólo quedan la
segunda y la tercera, y la tercera está calificada ahí como «No».

## Qué falta, concretamente

**P1 — Un preflight del llavero, que corra ANTES de crear la máquina.** Compara
`llavero.json` con lo que la máquina lanzadora tiene de verdad, y para un tipo con
`"llavero": true`:

- **si falta alguna, se NIEGA** y las nombra. No es un aviso: una máquina que nace
  sin su llavero es trabajo perdido y, en el caso del mini, mando perdido.
- imprime el remedio apuntando **fuera** de la máquina (dónde se pone cada una),
  que es donde está.
- ⚠ **Distingue «falta» de «vacía»**: una variable puesta a cadena vacía es peor
  que ausente, porque parece configurada.

Es la **regla 5 de escritura** del coordinador —*«un preflight comprueba estado
utilizable, no presencia, y crece con cada fallo»*— aplicada aquí: esta comprobación
se añade en el mismo commit que este documento… y **no se ha añadido**, que es por
lo que esto es un pendiente y no una nota histórica.

**P2 — Decidir cuáles son OBLIGATORIAS y cuáles opcionales.** Hoy `llavero.json` es
una lista plana. `TGL2_*` es legítimamente opcional (bot de staging); `DO_SSH_USER`
no lo es. Sin esa distinción, un preflight que exija las 17 sería un 🔴 permanente
—el aviso que sale siempre y se deja de leer— y acabaría desactivado.

**P3 — `DO_SSH_USER` debería tener defecto declarado, no heredado.** Que
`cfg()` caiga a `"root"` es razonable para un droplet pelado y equivocado para uno
con usuario de desarrollo. O lo aporta el **tipo** (como los otros 8 que sí aporta),
o el preflight lo exige. Elegir una de las dos es parte de esto.

## Cómo se comprueba que quedó bien

```bash
# el preflight caza el hueco real (con la variable quitada del entorno)
cd ~/src/digital-ocean-dropplet-auto-launching
env -u DO_SSH_USER python3 scripts/do_droplet.py launch prueba --type mini --seco
#   -> tiene que NEGARSE nombrando DO_SSH_USER, sin llamar a la API
```

Y los tests que pide (R17), ninguno de los cuales necesita tocar la API:

1. con el llavero completo, no estorba (no puede ser un 🔴 permanente);
2. con una obligatoria ausente, **se niega** y la nombra;
3. con una obligatoria **vacía**, se niega igual;
4. con una opcional ausente (`TGL2_*`), **deja pasar**;
5. un tipo sin `"llavero": true` no se ve afectado.

## Lo que NO hay que hacer

⚠ **No rellenar el llavero de este dev a mano y dar el problema por resuelto.** Eso
arregla esta máquina, y estas máquinas se destruyen. El agujero es que **nadie
comprueba**, y sobrevive a cualquier relleno manual.
