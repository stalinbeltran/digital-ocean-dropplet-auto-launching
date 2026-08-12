# Acceso SSH y consola web: diagnóstico

Notas de campo del 2026-08-12, sacadas de perseguir un droplet que dejó de
aceptar SSH. Complementan a [droplets-api.md](droplets-api.md), que cubre la API;
esto cubre **qué hacer cuando no puedes entrar**.

## El diagnóstico que separa "red" de "droplet"

El síntoma era el mismo en dos redes distintas: conexión rechazada al 22 con
~2,6 s de retardo. Con esa pinta es fácil concluir que un appliance corporativo
corta el SSH — y era falso. Lo que lo desmonta es comparar puertos:

| destino | ufw | resultado | lectura |
|---|---|---|---|
| droplet **:22**, **:443** | ALLOW | **RST** | el paquete llega al kernel y **nadie escucha** |
| droplet **:80**, **:8080** | denegado | **timeout** | ufw los descarta: el firewall está vivo |
| ICMP | — | responde | la máquina está encendida |
| `github.com:22` | — | banner en 0,3 s | tu salida al 22 funciona |

La clave: **ufw descarta (DROP), no rechaza**. Un puerto cerrado *detrás* de una
regla `ALLOW` devuelve RST porque contesta el propio kernel del droplet. Así que
si un puerto permitido da RST mientras uno denegado da timeout, el problema está
dentro del droplet. Si *todos* se comportan igual, mira la red.

```bash
python - <<'PY'
import socket, time
IP = "TU.IP.AQU.I"
for port in (22, 443, 80, 8080):          # 22/443 permitidos, 80/8080 no
    t0 = time.time()
    try:
        socket.create_connection((IP, port), timeout=12).close()
        print(f"{port}: conecta ({time.time()-t0:.1f}s)")
    except OSError as e:
        print(f"{port}: {e} ({time.time()-t0:.1f}s)")
PY
```

Contraprueba de red, por si acaso: si conectas al 443 de una **IP inexistente**
(`198.51.100.77`), hay un proxy transparente interceptando el 443 y cualquier
prueba contra ese puerto miente.

### Cuidado: un proxy hace que un puerto cerrado parezca abierto

La tabla de arriba se lee al revés si hay un proxy de por medio. Medido desde
una red doméstica con proxy transparente, contra un droplet cuyo ufw **descarta**
el 80:

| puerto | TCP | banner | qué era |
|---|---|---|---|
| 22 | conecta en **0,1 s** | `SSH-2.0-OpenSSH_9.6p1 Ub` | conexión real |
| 443, 80 | conectan en **0,0 s** | nada, luego timeout | el proxy, no el droplet |

El delator es el **tiempo**: 0,0 s es físicamente imposible contra Nueva York,
donde el RTT ronda los 100 ms. Una conexión instantánea la está contestando algo
de tu propia red. Por eso el lanzador no da un puerto por bueno hasta recibir el
banner `SSH-2.0-…`, y por eso conviene cronometrar las pruebas además de mirar
si conectan.

## La consola web depende de sshd

Es el error de concepto que más tiempo cuesta. Hay **dos** consolas y sólo una
funciona sin sshd:

- **Droplet Console** (la del navegador, la que se ofrece por defecto). La sirve
  el `droplet-agent`, que **se conecta al sshd del propio droplet**. Requisitos
  documentados: agente instalado (viene de serie desde agosto de 2021), el
  firewall aceptando SSH *en el puerto que use sshd*, y salida TCP a la metadata
  `169.254.169.254`, de donde el agente saca las claves efímeras de la sesión.
  **Si sshd no escucha, esta consola tampoco entra.**
- **Recovery Console** (VNC). Es la de verdad para rescates, y **exige
  autenticación por contraseña**. Las imágenes creadas sólo con claves SSH no
  tienen contraseña de root, así que primero hay que resetearla desde el panel.

Consecuencia práctica: **un droplet sin sshd sólo se rescata a mano**, con reset
de contraseña y VNC. De ahí el watchdog de [../../cloud-init.yaml](../../cloud-init.yaml).

### El agente lee `sshd_config`, no `ssh.socket`

> *"the Droplet Console connects on the first port defined in the Droplet's SSH
> daemon configuration, `/etc/ssh/sshd_config`"*

El agente parsea `Port`/`ListenAddress` y, si no encuentra ninguno, asume el 22.
En Ubuntu 24.04, donde los puertos reales viven en `ListenStream` de
`ssh.socket`, eso significa que **el agente puede apuntar a un puerto donde no
hay nadie**. Por eso el cloud-init escribe los puertos en los dos sitios:
`/etc/systemd/system/ssh.socket.d/` y `/etc/ssh/sshd_config.d/`.

## sshd que se muere solo en Ubuntu 24.04

Desde 22.10 sshd arranca por activación de socket: `Port` de `sshd_config` se
ignora y manda `ListenStream` de `ssh.socket`. El problema es que **openssh ha
ido y venido entre `ssh.socket` y el `ssh.service` clásico**, y una actualización
del paquete a mitad de vida del droplet puede dejar el servicio sin arrancar —
que es exactamente lo que vimos: `ss -lntp` mostraba el 22 y el 443 escuchando
justo al acabar cloud-init, y minutos más tarde no escuchaba nada.

Mitigación en `cloud-init.yaml`, sin depender de saber cuál de los dos gana:

- puertos declarados en `ssh.socket` **y** en `sshd_config.d`;
- `ssh-watchdog.timer`, que cada minuto mira si algo escucha en el 22 y si no
  hace `daemon-reload` + `restart ssh.socket`, con fallback a `restart ssh`, y
  registra el episodio en `/var/log/ssh-watchdog.log`.

Ese log es también la única evidencia post-mortem que tendremos la próxima vez:
revísalo antes de teorizar.

## Detalles menores que cuestan un rato

- **`DELETE /v2/droplets/{id}` es asíncrono.** Responde enseguida, pero el
  droplet sigue apareciendo en `GET /v2/droplets` unos segundos. Destruir y
  recrear con el mismo nombre sin esperar da un "ya existe" falso.
- **`status: active` no significa accesible.** Ni sshd escuchando, ni cloud-init
  terminado. El centinela `/var/lib/cloud/READY` sí lo significa.
- **Las claves se fijan en el arranque.** Registrar una clave nueva en la cuenta
  no la mete en los droplets que ya corren; sólo en los siguientes.

## Fuentes

- [How to Connect to Droplets with the Droplet Console](https://docs.digitalocean.com/products/droplets/how-to/connect-with-console/)
- [digitalocean/droplet-agent](https://github.com/digitalocean/droplet-agent)
- [How to Connect to Droplets with SSH](https://docs.digitalocean.com/products/droplets/how-to/connect-with-ssh/)
- [Socket-based activation en Ubuntu 24.04](https://dev.to/saishanmukkha/understanding-ssh-socket-based-activation-in-ubuntu-2404-28m)
