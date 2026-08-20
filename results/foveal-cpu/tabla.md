# foveal-cpu: s/epoca de foveal-vision en CPU: red y receta congeladas sobre el dataset real (bench-dirty1000-16). Mide la maquina sobre el trabajo que de verdad va a hacer.

Generada por `vast_instance.py sweep`. **No se edita a mano**: se rehace
entera a partir de los JSON de este directorio, que son el dato.

La columna que manda es **s/epoca (menos es mejor)**; la tabla va ordenada por ella, de
mas rapida a mas lenta.

| s/epoca (menos es mejor) | x vs. base | listo en | $/h | $/unidad | vCPU | CPU | RAM GB | ubicacion | fecha |
|---:|---:|---:|---:|---:|---:|---|---:|---|---|
| **21.87** | 2.82x | 3.8 min | 0.0489 | 0.00030 | 10 | Xeon E5-2630 v4 | 15.7 | South Korea, KR | 2026-08-20 |

## Como se lee

- **s/epoca (menos es mejor)** es el numero que se quiere bajar.
- **x vs. base** compara contra droplet DO s-2vcpu-4gb (61.762). Un 2,00x es la mitad de tiempo.
- **listo en** es lo que se tarda desde que se alquila hasta la primera
  unidad de trabajo: arrancar, subir el codigo e instalar. **Es un peaje
  que hay que amortizar**: una maquina el doble de rapida que tarda 6
  minutos en estar lista sale peor para un entrenamiento de tres epocas.
- **$/unidad** es el coste del trabajo, no de la hora. Es lo que decide si
  la maquina cara compensa: una que va el doble de rapida por el doble de
  precio empata en esta columna, y entonces lo unico que compras es tiempo
  de reloj.
- La base (droplet DO s-2vcpu-4gb) sale a **0.00061 $** por unidad, a 0.0357 $/h.
