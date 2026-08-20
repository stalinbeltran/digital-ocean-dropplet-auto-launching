# Servicios cloud para entrenar redes neuronales (pago por uso, sin compromiso mensual)

*Precios verificados el 19 de agosto de 2026. Los precios de GPU cloud cambian con frecuencia — conviene reconfirmar en la página oficial antes de decidir.*

Todos los servicios listados cumplen tus tres requisitos: (1) facturación por uso real (por segundo/minuto/hora, sin contrato mensual), (2) API para iniciar/administrar el entrenamiento programáticamente, y (3) capacidad de detener el recurso al terminar para no seguir pagando.

## 1. RunPod — mejor relación costo/flexibilidad para la mayoría de casos

Marketplace de GPUs con "Secure Cloud" (datacenters certificados) y "Community Cloud" (más barato, menor garantía). Facturación por segundo. API REST y SDK de Python para crear, monitorear y destruir pods automáticamente.

- RTX A5000 (24GB): $0.27/hr
- A40 (48GB): $0.44/hr
- RTX 3090 (24GB): $0.50/hr
- RTX A6000 (48GB): $0.53/hr
- RTX 4090 (24GB): $0.74/hr
- L40S (48GB): $0.99/hr
- A100 80GB (PCIe): $1.39/hr
- A100 80GB (SXM): $1.59/hr
- H100 PCIe (80GB): $2.89/hr
- H100 SXM (80GB): $3.29/hr
- H200 (141GB): $4.59/hr
- B200 (180GB): $6.79/hr

## 2. Vast.ai — el más barato (marketplace P2P, precios variables)

Subasta de GPUs de proveedores independientes y datacenters. Los precios fluctúan por oferta/demanda; hay instancias "on-demand" (garantizadas) e "interruptible" (más baratas, pueden ser interrumpidas). Facturación por segundo. API completa para lanzar/detener instancias.

- RTX 3070: desde $0.076/hr
- RTX 3090: desde $0.20/hr
- A10: desde $0.20/hr
- H100 SXM: desde ~$1.70–1.90/hr (según disponibilidad)
- Rango general publicado: desde $0.05/hr hasta varios $/hr según GPU

Nota: al ser marketplace, la fiabilidad y velocidad de red varían más que en proveedores "gestionados" como RunPod o Lambda.

## 3. Lambda Labs (Lambda Cloud) — buena relación precio/estabilidad para GPUs de gama alta

Infraestructura propia (no marketplace), pensada específicamente para deep learning. Facturación por hora/minuto. API y CLI oficiales.

- Tesla V100: $0.79/hr
- RTX A6000: $1.09/hr
- A10: $1.29/hr
- A100 SXM (40/80GB): $1.99/hr
- GH200: $2.29/hr
- H100 PCIe: $3.29/hr
- H100 SXM: $4.29/hr
- B200: $6.99/hr

## 4. Modal — serverless, ideal si quieres correr scripts de entrenamiento sin gestionar servidores

En vez de "levantar una VM", ejecutas funciones Python que Modal escala automáticamente y apaga solas al terminar (no necesitas detener nada manualmente). Facturación por segundo, con $30/mes de crédito gratis en el plan Starter (sin costo mínimo).

- T4: $0.59/hr ($0.000164/seg)
- L4: $0.80/hr
- A10: $1.10/hr
- A100 40GB: $2.10/hr
- A100 80GB: $2.50/hr
- L40S: $1.95/hr
- H100 SXM5: $3.95/hr
- H200 SXM: $4.54/hr
- B200: $6.25/hr
- B300: $7.10/hr

## 5. TensorDock — alternativa económica para H100/A100

Marketplace similar a Vast.ai pero con inventario más curado. Facturación por hora, API disponible.

- A100 80GB: desde $1.42/hr
- H100: desde $2.25/hr

## 6. Paperspace (por DigitalOcean) — buena documentación y notebooks integrados

Facturación por hora, API y CLI de Paperspace/DigitalOcean.

- A4000 (16GB): $0.76/hr
- A5000 (24GB): $1.38/hr
- A6000 (48GB): $1.89/hr
- A100 40GB: $3.09/hr
- A100 80GB: $3.18/hr
- H100 (80GB): $5.95/hr

## 7. Nubes grandes (AWS / GCP / Azure) — más caras, pero con más servicios alrededor

Solo recomendables si ya usas su ecosistema (S3, IAM, etc.) o necesitas cumplimiento empresarial. Instancias on-demand por hora, apagables vía API (EC2, Compute Engine, Azure ML). Como referencia, H100 on-demand:

- AWS (p5 instances): ~$6.88/hr por GPU (más caro por vCPU/RAM incluidos)
- Azure: ~$6.98–$7.89/hr por GPU

## Recomendación práctica

Para entrenar y pagar solo por el tiempo de cómputo, controlando todo por API:

- Si buscas el mejor precio con buena confiabilidad: **RunPod** (Secure Cloud).
- Si buscas el precio más bajo posible y toleras variabilidad: **Vast.ai**.
- Si prefieres no gestionar servidores en absoluto (solo subir el script y que se apague solo): **Modal**.
- Si necesitas GPUs de gama alta con infraestructura estable: **Lambda Labs**.

Todos ofrecen: crear el recurso vía API → correr el entrenamiento → destruir/detener el recurso vía API (o dejar que se apague solo, en el caso de Modal) → solo pagas por los minutos/segundos usados, sin mínimos mensuales.

Sources:
- [GPU Cloud Pricing | Per-Second H100, A100, RTX | Runpod](https://www.runpod.io/pricing)
- [H100 Cloud Pricing: Compare 53+ Providers (2026)](https://getdeploying.com/gpus/nvidia-h100)
- [Lambda Labs GPU Pricing | ComputePrices.com](https://computeprices.com/providers/lambda)
- [Vast.ai GPU Pricing | ComputePrices.com](https://computeprices.com/providers/vast)
- [Plan Pricing | Modal](https://modal.com/pricing)
- [$2.25/hr On-Demand H100s — TensorDock](https://www.tensordock.com/gpu-h100.html)
- [$1.42/hr A100 80GBs — TensorDock](https://www.tensordock.com/gpu-a100.html)
- [Paperspace Pricing | DigitalOcean Documentation](https://docs.digitalocean.com/products/paperspace/pricing/)

---

# Investigación de las APIs (20 de agosto de 2026)

Lo de arriba compara **precios**. Esto compara **APIs**: qué se puede pedir por programa, con qué
calidad está especificado y qué hace falta para el objetivo real — *crear varios servidores, correr
un benchmark en cada uno, medir y destruirlos*.

Método: se han descargado y leído las especificaciones OpenAPI donde existen, y se han llamado en
vivo los catálogos que no piden autenticación. Los números marcados **(medido)** salen de una
llamada real ese día; el resto sale de la especificación o de la documentación oficial.

## Tabla 1 — Comparativa de las APIs

| | Vast.ai | RunPod | Shadeform | Lambda | Prime Intellect | TensorDock | Modal | Paperspace |
|---|---|---|---|---|---|---|---|---|
| **Tipo de API** | REST | REST | REST (agregador) | REST | REST (agregador) | REST | SDK sobre gRPC | — |
| **Base URL** | `console.vast.ai/api/v0` | `api.runpod.io/v2` | `api.shadeform.ai/v1` | `cloud.lambda.ai/api/v1` | `api.primeintellect.ai/api/v1` | `dashboard.tensordock.com/api/v2` | (sin HTTP público) | deprecada |
| **OpenAPI publicado** | Sí, 3.1 · 66 rutas | Sí, 3.1 · 34 rutas | Sí, 3.0 (por endpoint) | Sí, 3.1 · v1.10.0 | Sí · 94 rutas | **No** | No | No |
| **Auth** | Bearer | Bearer | `X-API-KEY` | Bearer | Bearer | Bearer | token del SDK | — |
| **Catálogo sin autenticar** | **Sí** (`/bundles/`) | No | **Sí** (`/instances/types`) | No (401) | No (403) | No | No | — |
| **Precio en el catálogo** | Sí, oferta a oferta | Sí (`price`) | Sí (`hourly_price`, en centavos) | Sí (`price_cents_per_hour`) | Sí | Sí (`rateHourly`) | fijo por GPU | — |
| **Disponibilidad en el catálogo** | Sí, oferta a oferta | Sí (`availability` y por datacenter) | Sí, por región | Sí (`regions_with_capacity_available`) | Sí | Sí | n/a | — |
| **Facturación** | por segundo | por minuto (pods) | la del proveedor de debajo | por hora/minuto | por hora | por hora | por segundo | — |
| **Aislamiento** | contenedor (VM opcional) | contenedor | VM | VM | contenedor o VM | **VM** | contenedor | — |
| **Script de arranque** | `onstart` (4048 car.) | `dockerStartCmd` o plantilla | `base64_script` o Docker | **`user_data` cloud-init** | `image` + `envVars` | **objeto `cloud_init`** | código Python | — |
| **SSH** | claves por instancia | `startSsh` + claves de cuenta | `ssh_key_id` | `ssh_key_names` (obligatorio) | `sshKeyId` | `ssh_key` (obligatorio) | no lo hay (tiene `exec()`) | — |
| **Ejecutar comando por API** | Sí (`/instances/command/`, 512 car.) | No (SSH o logs) | No (SSH) | No (SSH) | No (SSH) | No (SSH) | Sí (`sandbox.exec`) | — |
| **Logs por API** | Sí (`request_logs`) | Sí (`/pods/{id}/logs`) | No | No | Sí (`/pods/{id}/log`) | No | Sí | — |
| **Autodestrucción** | no | no | **Sí: `auto_delete` por fecha o por gasto** | no | no | no | sí, la función acaba sola | — |
| **Tope de precio** | filtro `dph_total` | no | no | no | **`maxPrice`** | no | no | — |
| **Rate limit** | ~3 req/s por endpoint | `429` con `Retry-After` | no publicado | **1 req/s; lanzar 1 cada 12 s** | no publicado | 100 req/min | n/a | — |
| **Vale sólo con stdlib** | sí | sí | sí | sí | sí | sí | **no** (`pip install modal`) | — |

## Tabla 2 — Variedad que ofrece cada API

Los dos factores que se pidieron —variedad de vCPU y de tipos de GPU— y que además sirven de eje en
la propia comparativa de velocidad.

| | Modelos de GPU | GPUs por máquina | vCPU | Se elige el vCPU aparte de la GPU |
|---|---|---|---|---|
| **Vast.ai** | **59 distintos** (medido, en una muestra de 597 ofertas) | 1–16 (medido) | **1–768 núcleos, 35 valores distintos** (medido) | No se elige, viene con la oferta, pero **se filtra** por `cpu_cores`, `cpu_ghz` y `cpu_ram`; con este volumen de oferta equivale a elegirlo |
| **Shadeform** | 27 tipos en 276 configuraciones de 19 nubes (medido) | 0, 1, 2, 4, 8, 10, 16 (medido) | **4–480, 52 valores distintos** (medido) | No: se elige un `shade_instance_type` entero |
| **RunPod** | 34 en el enum de la v1 | `gpu.count`, sin tope documentado | fijo según el tipo de GPU | **No en pods de GPU.** La v1 al menos filtraba con `minVCPUPerGPU`; la v2 quitó incluso eso. `vcpuCount` sólo existe en pods de CPU |
| **TensorDock** | los del hostnode | mapa `gpus: {modelo: {count}}` | **`vcpu_count` libre, en pasos de 2** | **Sí, del todo**: vCPU, RAM y disco independientes de la GPU |
| **Prime Intellect** | agregado de varias nubes | `gpuCount` | **`vcpus` y `memory` en la creación** | **Sí** |
| **Lambda** | ~12 tipos fijos | 1 u 8 | fijo por tipo (`specs.vcpus`) | No |
| **Modal** | 11 (de T4 a B300) | `gpu="H100:4"` | **fracciones de núcleo**, mínimo 0,125 | Sí, pero es CPU de contenedor, no de máquina |

## Tabla 3 — El ciclo completo, endpoint por endpoint

La comparación que de verdad importa aquí: *crear -> esperar -> medir -> destruir*.

| Paso | Vast.ai | RunPod v2 | Shadeform | Lambda |
|---|---|---|---|---|
| Catálogo | `POST /api/v0/bundles/` | `GET /v2/catalog/gpus` | `GET /v1/instances/types` | `GET /api/v1/instance-types` |
| Clave SSH | `POST /api/v0/ssh/` | `PUT /v2/account/ssh-keys` | `POST /v1/sshkeys/add` | `POST /api/v1/ssh-keys` |
| Crear | `PUT /api/v0/asks/{id}/` | `POST /v2/pods` (201) | `POST /v1/instances/create` | `POST /api/v1/instance-operations/launch` |
| Esperar | `GET /api/v0/instances/{id}/` | `GET /v2/pods/{id}`: PROVISIONING, STARTING, RUNNING | `GET /v1/instances/{id}/info` | `GET /api/v1/instances/{id}` |
| Ejecutar | `PUT /api/v0/instances/command/{id}/` **o** SSH | SSH | SSH | SSH |
| Destruir | `DELETE /api/v0/instances/{id}/` | `DELETE /v2/pods/{id}` | `POST /v1/instances/{id}/delete` | `POST /api/v1/instance-operations/terminate` |
| Gasto | `GET /api/v0/charges/` | `GET /v2/billing/pods` | panel unificado | `GET /api/v1/audit-events` |

Las cuatro siguen el mismo patrón que ya tiene `do_droplet.py` con DigitalOcean: crear devuelve algo
en curso, hay que pollear hasta un estado terminal, y sólo entonces hay IP y SSH. **La lógica de
`launch` se reaprovecha casi entera**; lo que cambia son los nombres de los campos.

## APIs mal especificadas o muertas

- **TensorDock: API sin especificación formal.** No publica OpenAPI. La documentación son páginas
  HTML escritas a mano en `dashboard.tensordock.com/api/docs` que además **devuelven 403 a
  cualquier cliente que no mande un `User-Agent` de navegador** (comprobado: 403 con curl por
  defecto, 200 con UA de Chrome), así que no se puede leer desde un script. Los ejemplos de
  respuesta **se contradicen entre páginas**: la lista de instancias sale como
  `data.attributes.instances` en Getting Started y como `data.instances` en Instance Management.
  Los endpoints v0 del marketplace ya devuelven 404. Y la empresa fue **comprada por Voltage Park
  en marzo de 2025**, con el producto en migración. La API en sí es de las mejores del lote —VMs de
  verdad, cloud-init, vCPU/RAM/disco libres— pero integrarla es ingeniería inversa, no leer una
  spec.
- **Paperspace: API muerta.** Los endpoints de Gradient están **deprecados desde el 15 de julio de
  2024** y el producto se ha absorbido en DigitalOcean como Gradient GPU Droplets. O sea que lo que
  queda es la API de droplets de DigitalOcean, **la que este repo ya usa**. No es un proveedor
  nuevo: es el mismo, con GPUs. Se puede quitar de la lista de candidatos.
- **Modal: no tiene API HTTP para esto.** El control va por SDK de Python (TypeScript y Go en beta)
  sobre gRPC. Choca de frente con el objetivo 4 del proyecto (sólo stdlib, sin `pip install`). Y
  conceptualmente tampoco encaja: Modal ejecuta *una función tuya*, no te entrega *una máquina*, que
  para medir la velocidad de un servidor es justo lo que no quieres.
- **RunPod: cuidado con la versión.** La v1 (`rest.runpod.io/v1`) está **deprecada y se retira el 15
  de noviembre de 2026**. Casi todo el código de ejemplo que circula apunta ahí. Lo vivo es
  `api.runpod.io/v2`.
- **Nubes grandes (AWS/GCP/Azure):** la API existe y está bien documentada, pero la autenticación
  (SigV4 y equivalentes) no se hace con stdlib de forma razonable, y el precio dobla al resto. Fuera
  para este objetivo.

## Lo que hace especial a cada una para medir velocidad

- **Vast.ai ya trae el benchmark hecho.** Cada oferta llega con **100 campos**, entre ellos `dlperf`
  (puntuación de deep learning medida por Vast), `dlperf_per_dphtotal` (rendimiento por dólar),
  `total_flops`, `gpu_mem_bw`, `pcie_bw`, `disk_bw`, `inet_down`, `inet_up` y `reliability2`. Además
  hay un endpoint dedicado, `GET /api/v0/benchmarks/`, con puntuaciones reales por máquina y por
  modelo (`score`, `machine_id`, `model`, `num_gpus`), consultable con queries del tipo
  `score>1000`. **Se puede ordenar el mercado entero por rendimiento por dólar antes de gastar un
  céntimo.** Ninguna otra lo ofrece.
- **Shadeform compara proveedores, no sólo GPUs.** Un mismo H100 cuesta y rinde distinto en Lambda,
  Crusoe, Hyperstack o Nebius; con un solo cliente se lanza el mismo benchmark en 19 nubes (medido:
  imwt, massedcompute, lambdalabs, crusoe, paperspace, hyperstack, boostrun, latitude, scaleway,
  voltagepark, denvr, excesssupply, horizon, digitalocean, nebius, vultr, amaya, verda, phyntec). Su
  `auto_delete` por fecha **o por gasto** es el objetivo 2 de este proyecto implementado por el
  proveedor.
- **RunPod tiene la mejor especificación de todas.** El propio OpenAPI documenta el patrón
  leer-catálogo-y-luego-crear, y una tabla de qué códigos de error merece la pena reintentar: 422
  nunca, 400 prueba el siguiente candidato, 402 para del todo, 403 salta ése, 429 respeta
  `Retry-After`. Es el que menos sorpresas dará al programarlo.
- **Lambda es la que más se parece a lo que ya está escrito.** `user_data` con cloud-init, más
  `ssh_key_names`, más `launch`/`terminate`, es un calco de `do_droplet.py`: portar el lanzador
  sería casi cambiar la URL base. A cambio, pocos tipos, sin elección de vCPU, capacidad agotada a
  menudo, y un límite de **un lanzamiento cada 12 segundos** que estorba si quieres levantar diez
  máquinas a la vez para compararlas.

## Precios verificados en vivo contra la API de Vast.ai (medido el 2026-08-20)

Precio mínimo por GPU y hora sobre una muestra de 597 ofertas alquilables. Confirma y afina la
sección de precios de arriba, y enseña hasta dónde llega el rango:

| GPU | Desde $/GPU-h | | GPU | Desde $/GPU-h |
|---|---|---|---|---|
| RTX 2060 | 0,014 | | RTX 5090 (32 GB) | 0,310 |
| RTX 3060 | 0,022 | | A100 SXM4 (80 GB) | 0,363 |
| Tesla V100 (32 GB) | 0,027 | | RTX 6000 Ada (48 GB) | 0,388 |
| L4 (22 GB) | 0,030 | | L40S (45 GB) | 0,400 |
| RTX 3090 (24 GB) | 0,065 | | RTX A6000 (48 GB) | 0,481 |
| RTX 4090 (48 GB) | 0,134 | | RTX PRO 6000 (96 GB) | 0,851 |
| RTX PRO 4000 (24 GB) | 0,149 | | H100 NVL (94 GB) | 1,469 |
| CMP 170HX (64 GB) | 0,187 | | H100 SXM (80 GB) | 1,734 |
| A100 PCIE (80 GB) | 0,267 | | H200 NVL (140 GB) | 2,069 |
| RTX PRO 4500 (32 GB) | 0,308 | | B200 (179 GB) | 4,627 |

Dos avisos que salen de esos mismos datos: el marketplace tiene ofertas **absurdas en los dos
sentidos** (una Tesla T4 a 12 $/h, más cara que una H100), así que **hay que ordenar por precio y no
fiarse del modelo**; y las ofertas caras de un mismo modelo llegan a costar **4x** las baratas.

## Recomendación

**Empezar por Vast.ai.** Es a la vez lo más barato (por un orden de magnitud en la gama baja), lo
que más variedad ofrece (59 modelos de GPU, vCPU de 1 a 768, de 1 a 16 GPUs, 93 ubicaciones
medidas), y **lo único que ya publica métricas de rendimiento medido**, que es literalmente lo que
se quiere construir. Que el catálogo se consulte **sin clave** permite escribir y depurar el
selector de máquinas entero antes de abrir una cuenta.

Orden propuesto:

1. **Vast.ai** — el barrido barato. Cientos de combinaciones GPU/vCPU por céntimos la hora, y
   `dlperf_per_dphtotal` como línea base contra la que validar el benchmark propio.
2. **Shadeform** — la comparación entre nubes. El mismo benchmark en 19 proveedores con una sola
   clave, y `auto_delete` como red de seguridad.
3. **RunPod** — la referencia estable. API impecable y hardware de datacenter para repetir las
   medidas sin la varianza del marketplace.
4. **Lambda** — sólo si interesa reutilizar tal cual el `cloud-init.yaml` de este repo.

Lo bueno para este proyecto: **las cuatro son REST con Bearer y JSON**, así que caben en el `api()`
de `do_droplet.py` sin añadir una sola dependencia, y las cuatro tienen el mismo ciclo asíncrono
crear-pollear-destruir que aquí ya está resuelto. La forma natural es tratar cada proveedor como un
fichero de datos más, igual que `types/` y `services/`, en vez de escribir cuatro lanzadores.

**Lo que queda sin verificar:** los catálogos de RunPod, Lambda y Prime Intellect exigen clave, así
que sus precios y su variedad salen de la especificación y de la documentación, no de una llamada
real. En cuanto haya clave conviene repetir el ejercicio contra `GET /v2/catalog/gpus` y
`GET /api/v1/instance-types` y anotar aquí lo medido, como se ha hecho con Vast.ai y Shadeform.

Fuentes consultadas en esta investigación:
- [Vast.ai OpenAPI](https://docs.vast.ai/api-reference/openapi.json) y [docs.vast.ai](https://docs.vast.ai/)
- [Runpod REST API v2 OpenAPI](https://api.runpod.io/v2/openapi.json) y [docs.runpod.io/api-reference](https://docs.runpod.io/api-reference/overview)
- [Shadeform API reference](https://docs.shadeform.ai/api-reference/instances/instances-create) y [catálogo público](https://api.shadeform.ai/v1/instances/types)
- [Lambda Cloud API OpenAPI](https://cloud.lambda.ai/api/v1/openapi.json) y [docs.lambda.ai](https://docs.lambda.ai/public-cloud/cloud-api/)
- [Prime Intellect OpenAPI](https://api.primeintellect.ai/openapi.json) y [docs.primeintellect.ai](https://docs.primeintellect.ai/api-reference/pods/create-pod)
- [TensorDock API v2](https://dashboard.tensordock.com/api/docs/getting-started) y [Voltage Park adquiere TensorDock](https://www.voltagepark.com/blog/tensordock-joins-voltage-park-a-new-chapter-begins)
- [Gradient API deprecada](https://docs.digitalocean.com/reference/paperspace/gradient/)
- [Modal Sandbox, SDK de Python](https://modal.com/docs/reference/modal.Sandbox) y [Modal pricing](https://modal.com/pricing)
