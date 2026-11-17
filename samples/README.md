# Ejemplos

Escaneos **sintéticos** generados con `python scripts/make_samples.py` (no son escaneos reales):

- `sintetico_limpio.stl`: planta con paredes laterales conectadas y ruido mínimo, en milímetros.
- `sintetico_completo.stl`: todos los defectos a la vez (fragmentos, ala y solapas conectadas, capas
  superpuestas, agujeros, zonas incompletas, ruido), en **metros** y con orientación arbitraria.

`python scripts/make_samples.py --all` genera los ocho escenarios. Coloque aquí sus STL reales si
quiere versionarlos junto al código.
