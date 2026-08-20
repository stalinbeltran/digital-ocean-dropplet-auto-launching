# Resultados de los benchmarks

Aquí se commitea lo que se midió. Un subdirectorio por benchmark (el nombre del
descriptor en [`benchmarks/`](../benchmarks/)), y dentro:

- **un JSON por máquina medida**, `<fecha>-<vCPU>vcpu-<instancia>.json`. Es el
  dato: qué máquina era, qué costó, y el reporte tal cual lo dejó el benchmark.
- **`tabla.md`**, la comparativa. **No se edita a mano**: `vast_instance.py` la
  rehace entera a partir de los JSON cada vez que termina un barrido. Borrar una
  medida mala es borrar su fichero y volver a lanzar el barrido; así la tabla no
  puede acabar afirmando algo que ya no respalda ningún dato.

## Qué guarda cada JSON, y por qué

| campo | para qué |
|---|---|
| `maquina` | vCPU efectivas, modelo de CPU, RAM, GHz, ubicación, host y fiabilidad |
| `usd_hora`, `usd_medida`, `segundos_vivida` | lo que costó el número. Sin esto no se puede comparar rendimiento por dólar, que es la mitad de la pregunta |
| `metrica` | el número que se compara, extraído del reporte según el descriptor |
| `reporte` | el JSON entero del benchmark, sin tocar |
| `oferta`, `instancia` | para poder volver a la misma máquina del marketplace |

El reporte de `foveal-cpu` trae además `load_avg_before`: **un benchmark de CPU
miente bajo carga**, así que la carga del sistema antes de empezar va guardada en
vez de darla por cero.

## Dos medidas sólo se comparan entre sí si coinciden en el dato

Los reportes de foveal-vision llevan `window_dataset` justamente por eso. Un
cambio de dataset mueve el número aunque la máquina sea idéntica, y entonces la
comparación deja de significar nada. **Filtra por `window_dataset`, no supongas
que todas las filas de la tabla son comparables.**
