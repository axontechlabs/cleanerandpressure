# Parámetros

Todos los valores están en milímetros o grados (el programa convierte el STL a mm al cargarlo).
En la interfaz aparecen en el panel **Parámetros**; en la línea de comandos se pasan con
`--config archivo.json`:

```json
{
  "segment": {"max_angle": 68, "layer_tol": 1.0},
  "cut":     {"resolution": 0.6, "contour_inset": 1.0},
  "smooth":  {"intensity": 0.8, "texture_scale": 20},
  "solid":   {"thickness": 8, "mode": "offset", "side": "exterior"}
}
```

## Segmentación (`segment`)

| Parámetro | Por defecto | Efecto | Cuándo cambiarlo |
|---|---:|---|---|
| `seed_angle` | 35° | Pendiente máxima, respecto a la dirección plantar, de las caras "claramente plantares" desde las que se inicia la región. | Bájelo si una superficie inclinada ajena se toma como planta. |
| `max_angle` | 62° | Pendiente hasta la que crece la región. Marca dónde termina la planta y empieza el lateral del pie. | Súbalo (70–75°) para conservar más borde y arco medial; bájelo si sube demasiado por los laterales. |
| `crease_angle` | 80° | Ángulo entre caras vecinas que actúa como barrera de continuidad (pliegue). | Bájelo (50–60°) si una lámina espuria unida por un pliegue suave se cuela; súbalo si surcos reales cortan la planta. |
| `layer_tol` | 1,5 mm | Separación a partir de la cual dos superficies en la misma posición son capas distintas. | Bájelo si hay capas duplicadas muy próximas; súbalo con escaneos muy ruidosos. |
| `gap_tol` | 6 mm | Distancia máxima a la planta para conservar una isla (p. ej. un dedo) como *dudosa*. | Súbalo si se pierden los dedos; bájelo si se cuelan fragmentos cercanos. |
| `join_dz` | 4 mm | Salto de altura máximo entre la isla y la planta. | Igual que el anterior, en altura. |
| `bridge_gap` | 5 mm | Anchura máxima de los huecos entre regiones conservadas que se puentean con caras reales (surcos). `0` lo desactiva. | Súbalo si quedan grietas entre dedos y antepié. |
| `min_island_area` | 25 mm² | Las islas conservadas más pequeñas se descartan. | Súbalo si quedan motas. |
| `normal_radius` | 3 mm | Escala de suavizado de las normales antes de medir pendientes. | Súbalo con mucho ruido; bájelo para respetar detalle fino en el borde. |
| `grid` | 1 mm | Resolución de la rejilla de ocupación y visibilidad (solo por configuración). | Rara vez. |

## Contorno y remallado (`cut`)

| Parámetro | Por defecto | Efecto | Cuándo cambiarlo |
|---|---:|---|---|
| `resolution` | 0 (auto) | Tamaño de arista de la malla reconstruida. Automático = 0,8 × la arista mediana del escaneo, con un máximo de 1 mm (y no menos de L/700). | Bájelo para más detalle (más caras y tiempo); súbalo para mallas ligeras. |
| `contour_close` | 2 mm | Radio de cierre: une grietas y muescas del borde más estrechas que el doble del radio. | Súbalo para unir dedos; bájelo para respetar muescas reales. |
| `contour_open` | 1,5 mm | Radio de apertura: elimina picos y lenguas finas del borde. | Súbalo si el borde tiene salientes. |
| `contour_smooth` | 2 mm | Regularización del trazado del contorno. `0` la desactiva. | Súbalo para un contorno más redondeado. |
| `contour_inset` | 0 mm | Retranquea el borde hacia el interior. | Para descartar una banda de borde poco fiable. |
| `max_hole_area` | 0 | Área máxima de agujero que se rellena. `0` = todos (necesario para obtener un único contorno). | Si no quiere interpolar agujeros grandes (la superficie tendrá entonces varios contornos y el sólido, orificios pasantes). |
| `boundary_iters` | 10 | Iteraciones de alisado del borde con reproyección (solo por configuración). | Rara vez. |
| `layer_tol` | 1,5 mm | Si quedan dos capas en una celda más separadas que el doble de este valor, se toma la exterior y se avisa (solo por configuración). | Rara vez. |

Los planos de corte se indican aparte: `"planes": [{"point": [x, y, z], "normal": [nx, ny, nz]}]`,
en milímetros y en el marco del archivo; se conserva el lado hacia el que apunta la normal.

## Suavizado adaptativo (`smooth`)

El método por defecto (`multiescala`) mide la textura de cada escaneo y se ajusta a ella; no hay
que cambiar nada entre un pie descalzo y uno con calcetín.

| Parámetro | Por defecto | Efecto | Cuándo cambiarlo |
|---|---:|---|---|
| `method` | `multiescala` | `multiescala`: superficie lisa, elimina la textura y conserva el relieve real. `local`: regresión local conservadora (la versión anterior). | Use `local` si el escaneo ya es limpio y quiere tocarlo lo mínimo. |
| `intensity` | 0,7 | Nivel de alisado. Fija cuánta textura se supone en los tamaños grandes a partir del grano fino medido. `0` = no tocar; `1` = alisar al máximo aunque el escaneo sea limpio. | Súbalo si quedan ondulaciones; bájelo si prefiere conservar más forma. |
| `texture_scale` | 24 mm | Tamaño máximo de las irregularidades que se eliminan. Lo mayor se considera forma del pie. | Bájelo (16–20) para conservar más relieve suave; súbalo (30+) para quitar arrugas anchas, a costa de forma. |
| `detail_protection` | 0,5 | Cuánta evidencia hace falta para considerar que algo es relieve real. Más alto = se conservan más pliegues y relieves localizados. | Súbalo si los dedos pierden definición; bájelo si quedan hoyuelos o marcas. |
| `min_scale` | 4 mm | Todo detalle menor se elimina siempre (grano del sensor, puntos aislados), sin límite de desviación. | Bájelo (2–3) con escaneos de alta resolución; súbalo (5–6) con mucho grano. |
| `max_deviation` | 2 mm | Tolerancia de desviación respecto a la superficie sin grano. `0` = sin límite. | Ajústelo a la precisión que necesite garantizar. |

Solo para `method: "local"`:

| Parámetro | Por defecto | Efecto |
|---|---:|---|
| `radius` | 4 mm | Radio de influencia: las irregularidades de escala menor se tratan como ruido. Por encima de 6 mm altera el relieve de los dedos. |
| `iterations` | 3 | Pasadas del ajuste robusto. `0` desactiva el suavizado. |
| `curvature_preservation` | 0,5 | Reduce la intervención donde la curvatura coherente es alta. |
| `arch_protection` | 0,7 | Reduce la intervención y anula el desplazamiento de baja frecuencia en las zonas cóncavas. |
| `noise_floor` | 0,03 mm | Rugosidad local por debajo de la cual apenas se interviene. |

En el método local, `intensity` es la fracción del ajuste que se aplica y `max_deviation` se mide
respecto a la superficie de entrada.

El informe del suavizado muestra el análisis del escaneo: la textura medida, el grano fino
eliminado y, para cada tamaño de detalle, su nivel típico y en qué parte de la superficie se ha
conservado por ser relieve. El suavizado solo modifica la coordenada a lo largo de la dirección
plantar; el contorno y la triangulación no cambian.

## Sólido cerrado (`solid`)

| Parámetro | Por defecto | Efecto |
|---|---:|---|
| `thickness` | 5 mm | Espesor. Con base plana es el espesor **mínimo** (bajo el punto más saliente); con espesor uniforme es constante. |
| `mode` | `flat` | `flat`: base plana perpendicular a la dirección plantar. `offset`: copia de la superficie desplazada (espesor uniforme medido en esa dirección). |
| `side` | `interior` | `interior`: el material queda hacia el interior del pie (positivo de la planta). `exterior`: el material queda bajo la planta (plantilla o negativo). |
| `wall_rows` | 0 (auto) | Filas de triángulos de las paredes (solo por configuración). |

## Orientación

No es un parámetro numérico: **Invertir lado plantar** cambia el sentido de la dirección plantar y
**Planta = vista actual** la fija según la vista del visor. En la línea de comandos, `--flip`.
`--unit-scale` fuerza el factor a milímetros si la detección automática de unidades falla.
