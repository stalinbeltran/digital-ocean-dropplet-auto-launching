# Crear Droplets de DigitalOcean programáticamente

Referencia operativa para automatizar el ciclo de vida de Droplets (crear → esperar → usar → destruir).

> **Fuentes verificadas** (agosto 2026): especificación OpenAPI oficial de DigitalOcean
> (`github.com/digitalocean/openapi`, `specification/resources/droplets/`), docs de la API v2 y
> referencia de `doctl`. Los *slugs* concretos (tamaños, imágenes, regiones) **cambian con el tiempo**:
> no los hardcodees desde este documento, descúbrelos con los endpoints de la sección
> [Descubrir slugs](#descubrir-slugs-nunca-los-inventes).

---

## 1. Autenticación

Todo pasa por un **Personal Access Token (PAT)** creado en el panel: *API → Tokens/Keys → Generate New Token*.

```
Authorization: Bearer $DIGITALOCEAN_TOKEN
Content-Type: application/json
```

- **Base URL:** `https://api.digitalocean.com/v2/`
- Los tokens modernos son **scoped**. Para el flujo completo de auto-launch necesitas como mínimo:
  - `droplet:create`, `droplet:read`, `droplet:delete`
  - `actions:read` (para hacer polling del estado)
  - `ssh_key:read` (para resolver fingerprints/IDs)
  - Añade `firewall:*`, `reserved_ip:*`, `tag:*` sólo si el flujo los usa.
- Variables de entorno por convención:
  - `DIGITALOCEAN_TOKEN` → ejemplos de la API oficial
  - `DIGITALOCEAN_ACCESS_TOKEN` → la que leen **Terraform** y **doctl**

**Nunca** commitees el token. Va en `.env` (gitignoreado) o en el secret store del CI.

## 2. Límites y paginación

| Concepto | Valor |
|---|---|
| Rate limit | **5.000 req/hora** por token |
| Burst | **250 req/minuto** |
| Headers | `ratelimit-limit`, `ratelimit-remaining`, `ratelimit-reset`, y `retry-after` en los 429 |
| Paginación | `?page=N&per_page=M`, por defecto 20, **máximo 200** |
| Navegación | `links.pages` (`first`/`prev`/`next`/`last`) y `meta.total`; `links` sólo aparece si hay >20 resultados |

Detalle importante del rate limit: **no** se resetea en bloque al final de la hora. Cada request tiene su
propio temporizador y libera cupo una hora después de haberse hecho. Si haces polling agresivo, usa
backoff exponencial y respeta `retry-after`.

## 3. Descubrir slugs (nunca los inventes)

```bash
curl -sH "Authorization: Bearer $DIGITALOCEAN_TOKEN" \
  "https://api.digitalocean.com/v2/regions?per_page=200"

curl -sH "Authorization: Bearer $DIGITALOCEAN_TOKEN" \
  "https://api.digitalocean.com/v2/sizes?per_page=200"

# Imágenes de SO base (Ubuntu, Debian, Fedora…)
curl -sH "Authorization: Bearer $DIGITALOCEAN_TOKEN" \
  "https://api.digitalocean.com/v2/images?type=distribution&per_page=200"

# Imágenes propias (snapshots / custom)
curl -sH "Authorization: Bearer $DIGITALOCEAN_TOKEN" \
  "https://api.digitalocean.com/v2/images?private=true"
```

Equivalentes en CLI: `doctl compute region list`, `doctl compute size list`, `doctl compute image list-distribution`.

Notas:
- Un tamaño puede no estar disponible en todas las regiones → filtra por `regions[]` en `/v2/sizes`
  o por `sizes[]` en `/v2/regions`, y comprueba `available: true`.
- En `region` puedes pasar el prefijo (`nyc`) para que DO elija cualquier datacenter de esa región,
  o el slug exacto (`nyc3`).

## 4. Crear un Droplet

`POST /v2/droplets` → **202 Accepted** (aceptado, *no* creado todavía).

### Cuerpo de la petición

El schema es un `oneOf`: **`name`** (string, un droplet) o **`names`** (array, hasta **10** droplets de golpe).

| Campo | Tipo | Requerido | Default | Notas |
|---|---|---|---|---|
| `name` / `names` | string / array | Sí (uno de los dos) | — | `names` crea hasta 10 en una llamada |
| `size` | string | **Sí** | — | slug, p. ej. `s-1vcpu-1gb` |
| `image` | string \| integer | **Sí** | — | slug público, o ID numérico si es privada |
| `region` | string | No | — | slug o prefijo de región |
| `ssh_keys` | array\<int\|string\> | No | `[]` | IDs **o** fingerprints |
| `backups` | boolean | No | `false` | coste adicional |
| `backup_policy` | object | No | diario | sólo aplica si `backups: true` |
| `ipv6` | boolean | No | `false` | |
| `monitoring` | boolean | No | `false` | instala el agente de métricas |
| `tags` | array\<string\> | No | `[]` | se aplican tras la creación |
| `user_data` | string | No | — | cloud-config o script; **máx 64 KiB** |
| `volumes` | array\<string\> | No | `[]` | IDs de Block Storage |
| `vpc_uuid` | string | No | VPC por defecto | |
| `with_droplet_agent` | boolean | No | `true` | agente de consola web; errores ignorados |
| `public_networking` | boolean | No | `true` | |
| `private_networking` | boolean | No | `false` | **DEPRECADO** — usa `vpc_uuid` |

### Ejemplo (curl, tomado de la spec oficial)

```bash
curl -X POST \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $DIGITALOCEAN_TOKEN" \
  -d '{
        "name": "example.com",
        "region": "nyc3",
        "size": "s-1vcpu-1gb",
        "image": "ubuntu-20-04-x64",
        "ssh_keys": [289794, "3b:16:e4:bf:8b:00:8b:b8:59:8c:a9:d3:f0:19:fa:45"],
        "backups": true,
        "ipv6": true,
        "monitoring": true,
        "tags": ["env:prod", "web"],
        "user_data": "#cloud-config\nruncmd:\n  - touch /test.txt\n",
        "vpc_uuid": "760e09ef-dc84-11e8-981e-3cfdfeaae000"
      }' \
  "https://api.digitalocean.com/v2/droplets"
```

### Respuesta

- Creación individual → `{ "droplet": {...}, "links": { "actions": [...] } }`
- Creación múltiple → `{ "droplets": [...], "links": { "actions": [...] } }`

El objeto `droplet` recién creado viene con `status: "new"` y **`networks.v4` normalmente vacío**:
la IP todavía no está asignada. Ver la sección siguiente.

## 5. El ciclo asíncrono: esperar a que esté listo

El 202 sólo confirma que la petición se aceptó. Hay dos formas de esperar, y conviene usar ambas:

**a) Polling de la acción** — `links.actions[0].id` (o `.href`):

```
GET /v2/actions/$ACTION_ID     → { "action": { "status": "in-progress" | "completed" | "errored" } }
```

**b) Polling del droplet** — para obtener la IP:

```
GET /v2/droplets/$DROPLET_ID   → { "droplet": { "status": "new" | "active" | "off" | "archive", ... } }
```

Campos relevantes de la respuesta del droplet:

| Campo | Uso |
|---|---|
| `id` | identificador para todo lo demás |
| `status` | `new` → `active` es lo que esperas |
| `locked` | `true` mientras hay una acción en curso; no lances otra |
| `networks.v4[]` | cada entrada tiene `ip_address`, `type` (`public` \| `private`), `netmask`, `gateway` |
| `networks.v6[]` | idem para IPv6 |
| `size_slug`, `memory`, `vcpus`, `disk` | recursos efectivos |
| `vpc_uuid`, `tags`, `volume_ids` | asociaciones |

**La IP pública** = la entrada de `networks.v4` con `type == "public"`.

Patrón recomendado:

1. `POST /v2/droplets` → guarda `droplet.id` y `links.actions[0].id`.
2. Polling cada 5–10 s (con timeout de ~5 min) hasta `action.status == "completed"`.
3. `GET /v2/droplets/{id}` → extrae la IP pública.
4. Espera aparte a que SSH acepte conexiones: `status: active` **no** significa que cloud-init haya
   terminado ni que sshd esté escuchando. Reintenta el `ssh` hasta que conecte.
5. Si usas `user_data`, la señal real de "listo" es algo que ponga tu propio script
   (un fichero centinela, un webhook, `cloud-init status --wait`).

Trata `action.status == "errored"` como fallo terminal: el droplet puede existir pero estar inservible →
destrúyelo antes de reintentar, o dejarás recursos huérfanos facturando.

## 6. `user_data` / cloud-init

Máximo **64 KiB**. Dos formatos habituales:

```yaml
#cloud-config
users:
  - name: deploy
    ssh_authorized_keys:
      - ssh-ed25519 AAAA...
    sudo: ['ALL=(ALL) NOPASSWD:ALL']
    groups: sudo
    shell: /bin/bash
package_update: true
packages: [docker.io, docker-compose]
runcmd:
  - systemctl enable --now docker
  - touch /var/lib/cloud/READY
```

```bash
#!/bin/bash
set -euxo pipefail
apt-get update && apt-get upgrade -y
```

El log vive en el droplet en `/var/log/cloud-init-output.log` — primer sitio a mirar cuando el
arranque "funcionó" pero la máquina no hace lo que debía.

## 7. Claves SSH

```bash
# Listar (devuelve id y fingerprint)
curl -sH "Authorization: Bearer $DIGITALOCEAN_TOKEN" \
  "https://api.digitalocean.com/v2/account/keys"

# Registrar una nueva
curl -X POST -H "Authorization: Bearer $DIGITALOCEAN_TOKEN" -H "Content-Type: application/json" \
  -d '{"name":"ci-key","public_key":"ssh-ed25519 AAAA... ci@host"}' \
  "https://api.digitalocean.com/v2/account/keys"
```

Si creas un droplet **sin** `ssh_keys`, DigitalOcean genera una contraseña de root y la manda por email.
Para automatización eso es inútil y además inseguro: **siempre pasa `ssh_keys`**.

## 8. Destruir (la mitad que se olvida)

```bash
# Por ID
curl -X DELETE -H "Authorization: Bearer $DIGITALOCEAN_TOKEN" \
  "https://api.digitalocean.com/v2/droplets/$DROPLET_ID"          # → 204

# Por tag (destruye TODOS los que lleven ese tag)
curl -X DELETE -H "Authorization: Bearer $DIGITALOCEAN_TOKEN" \
  "https://api.digitalocean.com/v2/droplets?tag_name=ephemeral"   # → 204
```

Etiquetar cada droplet efímero al crearlo (`tags: ["ephemeral", "run:<id>"]`) hace que la limpieza
sea una sola llamada y sobreviva a que el proceso lanzador se muera.

Otras acciones sobre un droplet existente: `POST /v2/droplets/{id}/actions` con
`{"type": "power_off" | "power_on" | "reboot" | "shutdown" | "snapshot" | "resize" | "rebuild"}`.
Devuelven también una acción que hay que pollear.

## 9. Recursos relacionados

| Recurso | Endpoint | Para qué |
|---|---|---|
| Firewalls | `/v2/firewalls` | reglas de entrada/salida; se pueden asignar por `droplet_ids` o por `tags` |
| Reserved IPs | `/v2/reserved_ips` | IP estable que se reasigna entre droplets |
| VPCs | `/v2/vpcs` | red privada; `vpc_uuid` en la creación |
| Volumes | `/v2/volumes` | Block Storage, adjuntable en la creación |
| Projects | `/v2/projects` | agrupación/facturación; `doctl` lo expone como `--project-id` |
| Tags | `/v2/tags` | base para borrado y firewalls masivos |

Asignar el firewall **por tag** en vez de por `droplet_ids` evita una carrera: el droplet queda
protegido desde que nace, sin necesidad de una llamada extra tras la creación.

## 10. Recetas

### doctl

```bash
doctl auth init                     # o DIGITALOCEAN_ACCESS_TOKEN en el entorno

doctl compute droplet create web-01 \
  --image ubuntu-24-04-x64 \
  --size s-1vcpu-1gb \
  --region nyc1 \
  --ssh-keys 289794 \
  --vpc-uuid <uuid> \
  --tag-names ephemeral,web \
  --enable-monitoring \
  --user-data-file ./cloud-init.yaml \
  --wait \
  --format ID,Name,PublicIPv4 --no-header
```

`--wait` hace el polling por ti — es la forma más corta de un launch síncrono.
Flags relevantes: `--image` y `--size` son obligatorios; `--enable-backups`, `--enable-ipv6`,
`--enable-private-networking`, `--droplet-agent`, `--volumes`, `--project-id`, `--tag-name`/`--tag-names`,
`--user-data` (inline) / `--user-data-file`, `--backup-policy-{plan,weekday,hour}`.

Destruir: `doctl compute droplet delete web-01 --force` o `doctl compute droplet delete --tag-name ephemeral --force`.

### Python (`requests`)

```python
import os, time, requests

API = "https://api.digitalocean.com/v2"
H = {"Authorization": f"Bearer {os.environ['DIGITALOCEAN_TOKEN']}",
     "Content-Type": "application/json"}

def create_droplet(name, *, image, size, region, ssh_keys, user_data=None, tags=()):
    body = {"name": name, "image": image, "size": size, "region": region,
            "ssh_keys": list(ssh_keys), "tags": list(tags), "monitoring": True}
    if user_data:
        body["user_data"] = user_data
    r = requests.post(f"{API}/droplets", headers=H, json=body, timeout=30)
    r.raise_for_status()                      # 202
    data = r.json()
    return data["droplet"]["id"], data["links"]["actions"][0]["id"]

def wait_action(action_id, timeout=300, interval=8):
    deadline = time.time() + timeout
    while time.time() < deadline:
        a = requests.get(f"{API}/actions/{action_id}", headers=H, timeout=30).json()["action"]
        if a["status"] == "completed":
            return
        if a["status"] == "errored":
            raise RuntimeError(f"action {action_id} errored")
        time.sleep(interval)
    raise TimeoutError(f"action {action_id} no completó en {timeout}s")

def public_ip(droplet_id):
    d = requests.get(f"{API}/droplets/{droplet_id}", headers=H, timeout=30).json()["droplet"]
    return next(n["ip_address"] for n in d["networks"]["v4"] if n["type"] == "public")

def destroy(droplet_id):
    requests.delete(f"{API}/droplets/{droplet_id}", headers=H, timeout=30).raise_for_status()
```

Alternativa oficial: SDK `pydo` (`pip install pydo`).

### Node (fetch nativo)

```js
const API = "https://api.digitalocean.com/v2";
const H = {
  Authorization: `Bearer ${process.env.DIGITALOCEAN_TOKEN}`,
  "Content-Type": "application/json",
};

export async function createDroplet(body) {
  const res = await fetch(`${API}/droplets`, {
    method: "POST",
    headers: H,
    body: JSON.stringify({ monitoring: true, ...body }),
  });
  if (!res.ok) throw new Error(`${res.status} ${await res.text()}`);
  const { droplet, links } = await res.json();
  return { id: droplet.id, actionId: links.actions[0].id };
}
```

SDK oficial: `@digitalocean/do-js-sdk`. Alternativa muy usada: `dots-wrapper`.

### Terraform

```hcl
terraform {
  required_providers {
    digitalocean = { source = "digitalocean/digitalocean" }
  }
}

provider "digitalocean" {}   # lee DIGITALOCEAN_ACCESS_TOKEN

resource "digitalocean_droplet" "web" {
  name       = "web-01"
  image      = "ubuntu-24-04-x64"
  size       = "s-1vcpu-1gb"
  region     = "nyc1"
  ssh_keys   = [data.digitalocean_ssh_key.ci.id]
  vpc_uuid   = digitalocean_vpc.main.id
  monitoring = true
  tags       = ["ephemeral"]
  user_data  = file("${path.module}/cloud-init.yaml")
}
```

Usa Terraform cuando la infraestructura es **duradera y declarativa**; usa la API/`doctl` cuando el
droplet es **efímero y creado bajo demanda** (que es el caso de este repo).

## 11. Checklist de errores frecuentes

- [ ] Tratar el **202 como "ya está creado"**. No lo está: hay que pollear.
- [ ] Leer la IP de la respuesta del `POST`. Casi siempre viene vacía; léela del `GET` posterior.
- [ ] Asumir que `status: active` ⇒ SSH listo. No: reintenta la conexión, y espera a cloud-init aparte.
- [ ] Hardcodear slugs de imagen (`ubuntu-20-04-x64` acaba desapareciendo). Descúbrelos o pínchalos en config.
- [ ] Crear sin `ssh_keys` → contraseña de root por email, inservible en automatización.
- [ ] Elegir un `size` no disponible en la `region` elegida → 422.
- [ ] No etiquetar → droplets huérfanos facturando cuando el proceso lanzador falla.
- [ ] Polling en bucle cerrado → 429. Respeta `retry-after` y usa backoff.
- [ ] Ignorar `locked: true` y lanzar otra acción encima.
- [ ] Usar `private_networking` (deprecado) en vez de `vpc_uuid`.
- [ ] Commitear el token. `.env` fuera de git, siempre.
- [ ] Más de 10 nombres en `names` en una sola llamada.

## 12. Enlaces

- Referencia API v2: https://docs.digitalocean.com/reference/api/digitalocean/
- Spec OpenAPI (fuente de verdad): https://github.com/digitalocean/openapi
- Panel de Droplets: https://cloud.digitalocean.com/droplets
- Tokens: https://cloud.digitalocean.com/account/api/tokens
- `doctl`: https://docs.digitalocean.com/reference/doctl/
- Provider de Terraform: https://registry.terraform.io/providers/digitalocean/digitalocean/latest/docs
