# El almacén por dentro: qué ocupa, qué sobra, y si conviene un almacén de objetos (2026-10-03)

**Pedido por el dueño el 2026-10-03**, después de que el volumen `datos` se llenara al 100 %:
*«El repo git fue una idea para mantener la data que varias máquinas necesitaban, muchas veces
temporalmente. Analiza la data guardada, y dime qué porcentajes son innecesarios… Analiza si
podemos usar minio… Las máquinas contratadas en vast también deben poder guardar archivos ahí, y
leer (no deben poder borrar nada, pero pueden reemplazar sus propios archivos e insertar
nuevos).»*

Es un **análisis**: nada de lo de §2 se ha borrado y nada de lo de §3 se ha instalado.

## 1. Lo que ya se hizo ese día

| | antes | después |
|---|---:|---:|
| uso del volumen | 801 de 868 MB (100 %) | 375 MB (47 %) |
| repo `foveal-vision-data.git` | 835 MB, 7 packs + 91 MB sueltos | 392 MB, 1 pack |

1. **Reempaquetado a mano** (`git repack -a -d` sobre una copia en el disco raíz del mini; `fsck`
   y las cuatro ramas comprobadas idénticas antes de sustituir). Copias en
   `/var/tmp/fvd-orig.git` y `/var/tmp/fvd-new.git` del mini, pendientes de borrar.
2. **Se reempaqueta solo desde entonces** (`ed3d498`): `receive.unpackLimit 1` +
   `receive.autogc` + `gc.autoPackLimit 4`. Aplicado al repo vivo y en `CONFIG_ALMACEN`, con
   test que falla con la config anterior.

⚠ **Lo que se creía y NO era verdad**: que un `.jsonl.gz` nuevo por archivado costaba sus 4 MB
enteros porque git no puede hacer delta de un comprimido. Medido ese día con una conversación
real de 13 MB en dos versiones (80 % y 100 %): **gzip 4.020 KB, texto plano 4.032 KB** — git sí
hace delta, porque deflate produce los mismos bytes para el mismo prefijo. El coste estaba en el
**servidor** (packs completos y sueltos sin reempaquetar), no en el formato. Por eso **no** se
tocó el archivador.

## 2. Qué ocupa, y cuánto sobra

Medido el 2026-10-03 sobre el clon del dev tras `git gc` (398 MB en disco, toda la historia),
clasificando cada objeto por su ruta y por si sigue en `main` («vigente») o sólo en la historia
(«previa»):

| qué | MB | % | ¿hace falta? |
|---|---:|---:|---|
| pesos `.pt`, versiones **previas** | 118,8 | 29,9 % | **no**: pesos sobrescritos de barridos y pruebas |
| `preprocesado/` (`1k3`/`1k5`/`1k7-relu`) | 99,6 | 25,1 % | **casi seguro no**: ningún script de ningún repo lo nombra hoy; sale de un kernel congelado + un dataset que sí se guarda, o sea que se puede **re-derivar** |
| pesos `.pt` **vigentes** | 70,7 | 17,8 % | **casi todo no**: ~95 MB en crudo son de `sweeps/` y de `experimentos-cnn-resultados/*/pesos`; los tres aprobados de `inferencia.json` viven en `runs/` (~10 MB crudos) |
| conversaciones, versiones **previas** | 48,1 | 12,1 % | **no**: la última versión de cada una las contiene enteras |
| datasets `.npz` vigentes | 33,7 | 8,5 % | **sí** en su mayoría (no se pueden regenerar idénticos); unos pocos sin ninguna referencia en código (`limites300-…`, `dirty1000-…-r20260823`, `synth-01-holdout-b16`, `dirty-paragraphs-*`) |
| conversaciones vigentes | 19,6 | 4,9 % | **sí**: es el archivo de depuración pedido el 2026-08-31 |
| métricas, JSON, árboles, resto | 7,0 | 1,7 % | **sí**: pesan poco y son el resultado |

**En resumen** *(los dos últimos grupos son estimación: «nadie lo nombra» se midió con grep
sobre los repos de `~/src`, no se preguntó a quien lo usó)*:

| | % |
|---|---:|
| **sobra seguro** — versiones previas de pesos y de conversaciones | **≈ 42 %** |
| **sobra probablemente** — `preprocesado/`, pesos de barridos/pruebas no aprobados, datasets sin uso | **≈ 41 %** |
| **hace falta** — datasets en uso, pesos aprobados, conversaciones actuales, métricas | **≈ 17 %** |

⚠ **Quitar cualquiera de esto exige reescribir la historia**, que el almacén prohíbe a propósito
(«nadie borra»), cambia todos los hashes y obliga a re-clonar en cada máquina. Por eso la
pregunta de fondo no es «qué borro», sino **«qué no debería haber entrado en git»**: §3.

## 3. ¿Un almacén de OBJETOS para lo temporal? Sí — pero MinIO ya no

La idea del dueño es la correcta: git sirve para lo que **se versiona y se lee entero** (código,
métricas, reportes, datasets pequeños y congelados). Es malo para **binarios temporales**
(pesos, preprocesados, logs de flota), porque **no olvida** y porque su modelo de permisos es
«empujas a una rama o no»: no hay forma de decir *«esta máquina puede escribir sus ficheros pero
no tocar los de otra»*.

Un almacén de objetos S3 sí lo dice, y trae **ciclo de vida** (*«borra lo de `temporal/` a los
30 días»*, *«guarda las versiones viejas 7 días»*).

### 3.1 MinIO: no

⚠ **MinIO Community dejó de existir como proyecto mantenido** *(leído el 2026-10-03, no
comprobado contra el repo)*: en mayo de 2025 quitó de la consola la gestión de usuarios,
políticas y ciclo de vida; en octubre de 2025 dejó de publicar binarios e imágenes (sólo código
fuente); el 2026-02-12 archivó el repositorio («no longer maintained»). Montarlo hoy sería
compilar desde fuente un servidor expuesto a internet **sin parches de seguridad**. Y el mini
tiene **458 MB de RAM** (medido ese día), poco para un servidor S3 junto al bot.

### 3.2 Las dos alternativas que sí cumplen

| | **DigitalOcean Spaces** (gestionado) | **Garage** (en el mini) |
|---|---|---|
| coste | **5 $/mes** por 250 GB *(precio de memoria, comprobar)* | 0 $ extra; el disco es el volumen |
| mantenimiento | ninguno | parches, TLS, puerto abierto, RAM del mini |
| ciclo de vida | sí (reglas S3 de expiración) | **no comprobado** qué reglas soporta |
| claves por máquina con permisos acotados | sí, claves por bucket *(comprobar el grano exacto)* | sí, claves por bucket |
| sobrevive a rehacer el mini | **sí**, no depende de ninguna máquina | sólo el volumen |

**Recomendación: Spaces**, por la misma razón por la que el almacén es un volumen y no un disco:
lo que guarda datos no puede depender de que una máquina de 512 MB esté viva y parcheada.

### 3.3 Cómo se cumple *«leer todo, escribir lo suyo, no borrar nada»*

S3 no tiene «dueño de un objeto» como permiso, pero se consigue con **dos piezas**:

1. **Versionado del bucket encendido.** Con él, sobrescribir un fichero **no destruye** el
   anterior (queda como versión no actual) y un `DELETE` sólo pone una marca. «Reemplazar sus
   propios archivos» deja de ser peligroso, y la regla de ciclo de vida decide cuándo se
   eliminan las versiones viejas (p. ej. 30 días).
2. **Una clave por máquina de Vast, de vida corta**, con lectura en todo el bucket y escritura
   **sólo** bajo `maquinas/<id-instancia>/`. Sin `DeleteObject` ni `DeleteObjectVersion`. La
   crea quien alquila la máquina y la revoca quien la destruye — la misma pareja de R11 que ya
   cumple el vigilante.

⚠ Si el proveedor sólo permite claves de grano «bucket entero», el punto 2 se degrada a «escribe
en cualquier sitio, pero no puede borrar», y el versionado sigue impidiendo perder nada. Hay
que **comprobarlo antes** de decidir.

### 3.4 Qué iría a cada sitio, propuesto

| va a… | qué |
|---|---|
| **git (almacén actual)** | métricas, `summary.json`, reportes, manifiestos, datasets pequeños y congelados, `inferencia.json`, conversaciones |
| **objetos, permanente** | pesos **aprobados** (`inferencia.json` pasa a apuntar a una URL) |
| **objetos, con caducidad** | pesos de barridos y pruebas, `preprocesado/`, logs de flota, `temporal/` |

Y con eso la historia de git deja de crecer con binarios, que es el 83 % de §2.

## 4. Lo que NO está hecho y hay que decidir

1. **Si se monta Spaces** (5 $/mes, decisión de gasto del dueño) y cómo se reparten las claves.
   Por R11, el comando que crea claves para Vast va en el mismo commit que el que las revoca, y
   `cerrable.mjs` tendría que contar las claves vivas.
2. **Si se reescribe la historia** del repo de datos para recuperar el ≈ 42–83 % de §2. Choca
   con «nadie borra» y exige re-clonar en todas las máquinas.
3. **Borrar las dos copias de seguridad** de `/var/tmp` del mini (~1,2 GB del disco raíz).

## 5. Decidido por el dueño el mismo 2026-10-03

- **No se contrata Spaces** (ni ningún almacén de objetos, por ahora). §3 queda como análisis.
- **Historia compactada** (§4.2): cada rama es un solo commit con su contenido de ese día, árboles
  comprobados idénticos; las otras ramas apuntan a la `main` nueva para que un clon viejo sea
  rechazado al empujar. Almacén: 375 MB → **263 MB (33 %)**. Clones del dev, `~/ws/tema-2` y mini
  reajustados. Lo que queda es contenido **vigente**: el ≈41 % «probable» de §2 sigue ahí.
- **Copias de `/var/tmp` del mini borradas** (§4.3).
