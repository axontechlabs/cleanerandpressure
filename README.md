# 3D Scan Cleaner — Simulación bilateral

Aplicación web local para convertir el STL de un escaneo plantar en un sólido cerrado y limpio:
aísla la planta del pie, elimina la geometría que no le pertenece (aunque esté conectada o
superpuesta), genera un contorno limpio, suaviza de forma adaptativa sin aplanar el arco y
construye una malla estanca validada, lista para CAD o fabricación.

Por defecto el corte es de **plantilla**: talón completo, arco, antepié y los cinco dedos, más la
copa del talón, la pestaña medial bajo el arco y algo de pared lateral, hasta una línea de recorte
por altura sobre el suelo. El programa mide qué parte de cada zona ha conservado y, si falta algo,
reajusta los parámetros y vuelve a cortar. Opcionalmente, **Claude (IA)** revisa el corte y el mapa
de presión, propone parámetros y se repite el cálculo (ver [docs/IA.md](docs/IA.md)).

Incluye además un **modo bilateral**: limpia los escaneos de los dos pies de una persona por separado
y simula la bipedestación estática sobre ambos a la vez, con un mapa de presión plantar bilateral al
estilo de la baropodometría (ver [docs/BILATERAL.md](docs/BILATERAL.md)).

- **Procesamiento**: Python + NumPy + SciPy (sin librerías compiladas propias ni licencias copyleft).
- **Interfaz**: servidor local Starlette/Uvicorn + visor 3D WebGL2 propio (sin CDN, funciona sin conexión).
- **Entrada**: STL binario o ASCII. **Salida**: STL, OBJ o PLY.

> Estado: validado con escaneos **sintéticos** de verdad-terreno conocida (8 escenarios de defectos).
> Todavía **no** se ha validado con un STL real; véase [Limitaciones](#limitaciones-conocidas).

## Inicio rápido (macOS / Linux)

Requisitos: Python 3.10 o superior (`python3 --version`). En macOS: `brew install python` o el
instalador de python.org.

```bash
git clone https://github.com/axontechlabs/cleanerandpressure.git
cd cleanerandpressure
chmod +x run.sh install.sh
bash run.sh
```

`run.sh` crea un entorno virtual `.venv`, instala las dependencias la primera vez y abre
`http://127.0.0.1:8000` en el navegador. Para detenerla: `Ctrl+C`.

Instalación manual equivalente:

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m app.main            # opciones: --port 8080  --no-browser  --host 0.0.0.0
```

### GitHub Codespaces (ejecutar desde un enlace, sin instalar nada)

Con el código ya subido a GitHub, abra este enlace con su cuenta de GitHub:

**https://codespaces.new/axontechlabs/cleanerandpressure**

Pulse *Create codespace*. GitHub prepara una máquina en la nube, instala las dependencias y arranca
la aplicación sola (configurado en `.devcontainer/devcontainer.json`); al terminar ofrece abrir el puerto 8000 en una
pestaña nueva del navegador (*Open in Browser*). Si no aparece el aviso, abra la pestaña *Ports* y
pulse el icono del globo junto al puerto 8000. El procesamiento se ejecuta en el Codespace; el visor
3D usa la GPU de su navegador. Para volver a arrancarla manualmente dentro del Codespace:

```bash
python -m app.main --host 0.0.0.0 --no-browser
```

Al terminar, detenga o borre el Codespace en https://github.com/codespaces para no consumir cuota.

### Docker

```bash
docker build -t cleanerandpressure .
docker run --rm -p 8000:8000 -v "$PWD/workspace:/app/workspace" cleanerandpressure
```

## Uso de la interfaz

1. **Cargar STL…** (o arrastrar el archivo al visor). También puede cargar un *ejemplo sintético*.
   Se guarda una copia intacta del archivo en `workspace/` y se trabaja siempre sobre otra copia.
2. **▶ Procesamiento automático completo**, o bien las operaciones una a una:

   | Botón | Qué hace |
   |---|---|
   | 1. Analizar STL | Vértices, caras, dimensiones, componentes, bordes abiertos, caras degeneradas y duplicadas, errores manifold, normales incoherentes, autointersecciones, capas superpuestas y densidad de polígonos. |
   | 2. Segmentar superficie plantar | Clasifica cada cara: **conservar** (verde), **dudosa** (amarillo) o **eliminar** (marrón). En modo plantilla añade las paredes (copa del talón, pestañas) hasta la línea de recorte. |
   | 3. Eliminar geometría no deseada | Extrae la planta según la segmentación (y sus correcciones manuales). |
   | 4. Recortar y generar contorno | Aplica los planos de corte confirmados, rellena agujeros, regulariza el contorno y remalla con densidad uniforme. |
   | 5. Suavizar | Suavizado adaptativo multiescala: analiza la textura del escaneo y deja la superficie lisa conservando el relieve real. |
   | 6. Crear sólido cerrado | Base plana o espesor uniforme, con el espesor indicado. |
   | 7. Validar malla | Comprobaciones de validez y métricas de calidad. |
   | 8. Exportar | STL / OBJ / PLY de cualquier etapa. |

3. **Etapas (comparación)**, a la derecha: marque una o varias etapas para verlas o superponerlas
   (con opacidad ajustable). **Volver** regresa a esa etapa y descarta las posteriores.
4. **Informe**, a la derecha: resultados del análisis, avisos, lista de comprobaciones y métricas.

### Visor

Arrastrar: girar · botón derecho o Mayús+arrastrar: desplazar · rueda: zoom. Vistas *Superior,
Inferior, Frontal, Trasera, Lateral* y *Encajar*. Casillas para ver la **malla**, los **bordes
abiertos**, las **normales**, los colores de **segmentación** y el **mapa de desviación**
(cuánto ha desplazado el suavizado cada punto, en color y con signo). La desviación numérica
respecto al escaneo original (media, RMS, P95 y máxima) aparece en el informe de validación.

### Corrección manual de la segmentación

Con la etapa *Original* visible y la segmentación calculada:

- **Pincel conservar / eliminar**: pinta las caras visibles bajo el cursor (radio ajustable en mm).
- **Grupo conservar / eliminar**: un clic cambia todo el grupo conexo de caras con la misma etiqueta
  (un fragmento, una capa, una isla dudosa).
- **Elegir planta**: un clic indica cuál es la superficie plantar y se vuelve a segmentar desde ahí
  (útil si la automática ha elegido otra superficie).
- **Deshacer / Rehacer** (también Ctrl/Cmd+Z y Mayús+Ctrl/Cmd+Z) y **Borrar correcciones**.
- **Invertir lado plantar** y **Planta = vista actual**: corrigen la orientación si la cara plantar
  detectada es la contraria o la malla es ambigua.

Las correcciones se conservan al cambiar tolerancias y volver a segmentar: solo se corrige la zona
problemática, sin repetir el resto del trabajo. Las regiones dudosas se conservan por defecto y
generan un aviso; es preferible revisarlas a perder anatomía real.

### Plano de corte

Active el plano, elija a qué eje es perpendicular (largo, ancho o plantar), muévalo con *Posición*
e inclínelo con *Inclinación A/B*. La parte que se eliminará deja de verse en vivo. **Confirmar
corte** lo aplica (puede encadenar varios); **Cancelar** lo descarta; **Quitar cortes** los elimina.
Los triángulos atravesados se dividen: el borde resultante es recto y continuo.

### Exportación

Elija etapa, formato y orientación (*la del escaneo* o *base apoyada en Z=0*). El **sólido final
solo se exporta si ha superado todas las comprobaciones**; en caso contrario la aplicación lo
indica y no lo presenta como válido. *Archivo original intacto* descarga la copia sin modificar.

## Corte de plantilla y revisión por IA

- **Tipo de corte** (Parámetros → Segmentación y corte de plantilla): `plantilla` (por defecto) o
  `planta` (solo la planta, como antes). Alturas de la línea de recorte: copa del talón 15 mm,
  pestaña medial = altura del arco + 6 mm, pared lateral 10 mm, antepié 6 mm, dedos 4 mm.
- El informe muestra la **cobertura** por zonas (talón, mediopié, antepié, dedos), la altura de
  pared alcanzada y los reajustes automáticos que se han hecho.
- **Al abrir el programa** se conecta con Claude: si no hay clave guardada o no funciona, una ventana
  la pide, la comprueba y la guarda en este ordenador. Modelo por defecto: Claude Sonnet 5.5.
- **La IA forma parte del proceso.** *▶ Procesamiento automático completo* (un pie) y *Procesar ambos
  escaneos* (bilateral) cortan, y Claude revisa el corte y reajusta los parámetros hasta darlo por
  bueno (6 rondas como máximo) antes de suavizar y crear el sólido. *Simular* hace lo mismo con el
  mapa de presión. Sin clave, el proceso se detiene y lo indica; la casilla *Sin IA (solo pruebas)*
  permite procesar sin ella.
- Necesita una clave de la API de Claude con créditos: se compran en la Claude Console
  (`platform.claude.com` → *Settings → Billing → Buy credits*; la clave, en *API keys*, ámbito
  *Default workspace*). Detalles, coste aproximado y cómo guardar la clave sin subirla a GitHub:
  [docs/IA.md](docs/IA.md).

```bash
export ANTHROPIC_API_KEY=sk-ant-...      # o en el archivo .env (no se sube a GitHub)
.venv/bin/python cli.py escaneo.stl -o plantilla.stl --vistas vistas.png      # con IA (por defecto)
.venv/bin/python cli_bilateral.py izq.stl der.stl --peso 70 -o salida/          # con IA (por defecto)
.venv/bin/python cli.py escaneo.stl -o plantilla.stl --sin-ia                  # sin IA (pruebas)
```

## Modo bilateral (dos pies)

Botón **Modo bilateral (dos pies) →** en la cabecera, o directamente `http://127.0.0.1:8000/bilateral`.

1. Cargue el STL del pie izquierdo y el del derecho (o *Ejemplo sintético (pareja)*).
2. **Procesar ambos escaneos**: cada pie pasa por el mismo procesamiento automático que en la
   aplicación de un pie y se obtienen dos STL limpios e independientes, validados.
3. Introduzca el **peso**, la **estatura** (opcional) y el **reparto izquierdo/derecho**. El 50/50 es
   una hipótesis, no una medición. Si conoce la proyección del centro de masas, elija ese modo y el
   reparto resultará del equilibrio de momentos.
4. **Colocar pies y ver en 3D**: postura media por defecto (17 cm entre talones, 7° de apertura por
   pie). Si indica la estatura, la separación se estima a partir de ella y se comprueba que la
   longitud de los pies es coherente con la estatura (escala del escaneo). Se detecta qué pie es
   cada escaneo y se avisa si están cruzados.
5. **Simular bipedestación bilateral**: muestra un único mapa con los dos pies en escala común de kPa,
   los mapas individuales, el 3D coloreado y el informe del conjunto, de cada pie y comparativo.
   El modelo, a partir del escaneo sin carga:
   - mide el arco interno (altura, ápice) y el borde lateral del mediopié;
   - hace descender el arco medial y la columna lateral con la carga (por defecto, según la longitud
     del pie; puede escribir la caída del navicular medida). La banda lateral del mediopié une el
     talón con el antepié, como en una huella estática normal;
   - asienta cada uno de los cinco dedos en su articulación metatarsofalángica hasta que apoya y lo
     aprieta contra el suelo con el momento flexor: los dedos aparecen en el mapa con carga ligera;
   - da a cada dedo la sección redondeada y la falange proximal algo levantada que los escaneos
     (dedos juntos, calcetín) no muestran, con un pulpejo blando y poco reparto lateral: cada dedo
     aparece como una mancha separada, como en una plataforma de presión, y no como una franja.
6. Exportación: dos STL limpios, tres PNG, CSV de presiones y JSON de resultados.

Sin interfaz:

```bash
.venv/bin/python cli_bilateral.py izquierdo.stl derecho.stl --peso 70 -o resultados/
.venv/bin/python cli_bilateral.py izquierdo.stl derecho.stl --peso 70 --estatura 165 -o resultados/
.venv/bin/python cli_bilateral.py izquierdo.stl derecho.stl --peso 70 --caida-navicular 8 -o resultados/
```

El modelo físico, sus hipótesis y sus limitaciones están en [docs/BILATERAL.md](docs/BILATERAL.md).

## Línea de comandos

```bash
.venv/bin/python cli.py escaneo.stl -o planta_solida.stl
.venv/bin/python cli.py escaneo.stl -o planta.obj --thickness 8 --mode offset --report informe.json
.venv/bin/python cli.py escaneo.stl -o suave.ply --stage suavizado --config parametros.json
```

Código de salida: `0` si el sólido es válido, `2` si no lo es (no se exporta salvo `--force`),
`1` si hay un error. `--config` admite `{"segment":{}, "cut":{}, "smooth":{}, "solid":{}}` con los
parámetros de [docs/PARAMETROS.md](docs/PARAMETROS.md).

## Cómo funciona

El detalle y la comparación de alternativas están en [docs/INVESTIGACION.md](docs/INVESTIGACION.md).

1. **Limpieza y orientación.** Se sueldan vértices, se quitan caras degeneradas y duplicadas y se
   propaga una orientación coherente. La dirección plantar se estima a partir de la normal media
   ponderada por área (con alternativas para mallas cerradas o con normales invertidas) y el pie
   se lleva a un marco propio; por eso el resultado no depende de la orientación ni de las unidades
   del archivo (metros, centímetros o milímetros se detectan y se convierten a mm).
2. **Extracción plantar.** Combina pendiente respecto a la dirección plantar (con normales
   suavizadas), barreras de continuidad (pliegues y aristas no manifold, por donde suelen unirse
   las láminas espurias), visibilidad desde el escáner y una rejilla de ocupación que impide aceptar
   dos capas en la misma posición. La región crece desde la planta con histéresis, de modo que el
   arco se conserva por ser continuo con el resto, no por su altura. No hay filtro de altura ni se
   elige "el componente más grande".
3. **Contorno y remallado.** La planta segmentada es un campo de alturas visto desde la dirección
   plantar. Se remuestrea con interpolación baricéntrica exacta de los triángulos originales, los
   agujeros se rellenan con un carenado biarmónico, la huella se regulariza morfológicamente y el
   borde se lleva a una curva suave con precisión de subcelda.
4. **Suavizado adaptativo multiescala.** La superficie se descompone por tamaño de detalle con
   *splines* de placa delgada (no encogen ni aplanan). El grano menor de 4 mm y los puntos
   aislados se eliminan siempre. En cada banda mayor se mide la textura típica de ese escaneo:
   donde la banda está a ese nivel es textura (ruido del sensor, tejido del calcetín, arrugas) y
   se elimina; donde lo supera con claridad y de forma extensa es relieve real (dedos, surcos) y
   se conserva. Un escaneo rugoso se alisa más y a mayor escala; uno limpio casi no se toca.
   Solo se mueve la coordenada plantar, así que el contorno no cambia.
5. **Sólido.** La superficie anatómica se conserva vértice a vértice; se añaden una base (plana o
   paralela) y paredes verticales. Al ser un campo de alturas con paredes verticales, el sólido no
   puede autointersecarse por construcción, y aun así se comprueba.
6. **Validación.** Estanqueidad, aristas y vértices manifold, caras degeneradas, coherencia de
   normales, un único componente, volumen positivo, característica de Euler, autointersecciones
   (prueba exacta arista-triángulo) y que la superficie anatómica no se haya alterado.

## Estructura del repositorio

```
app/                    servidor web local y visor
  main.py               arranque (python -m app.main)
  server.py             API HTTP
  bilateral_api.py      API del modo bilateral
  static/               index.html, app.js, viewer.js (WebGL2), style.css; bilateral.html/.js/.css
core/                   núcleo geométrico (NumPy/SciPy)
  mesh.py               malla indexada y topología
  mesh_io.py            lectura STL; escritura STL/OBJ/PLY
  mesh_cleaning.py      soldado, degeneradas, duplicadas, orientación coherente
  mesh_analysis.py      etapa 1: análisis
  orientation.py        marco plantar
  raster.py             rasterización / z-buffer
  segmentation.py       etapa 2: extracción plantar
  insole.py             corte de plantilla: suelo, marco anatómico, línea de recorte, paredes y cobertura
  ai_client.py          cliente de la API de Claude (solo biblioteca estándar)
  ai_views.py           vistas de diagnóstico del corte (PNG) que analiza la IA
  ai_tuner.py           bucles de revisión por IA del corte y del mapa de presión
  mesh_cutting.py       etapa 3a: corte exacto por plano
  surface_remesh.py     etapa 3b: contorno, agujeros y remallado
  smoothing.py          etapa 4: suavizado adaptativo
  mesh_reconstruction.py etapa 5: sólido cerrado
  mesh_validation.py    comprobaciones de validez
  metrics.py            desviación y preservación
  pipeline.py           orquestación por etapas
  bilateral.py          modo bilateral: geometría de cada pie, colocación y simulación de contacto
  bilateral_session.py  modo bilateral: dos limpiezas independientes + simulación conjunta
  pressure_maps.py      mapas de presión en PNG
  synthetic.py          escaneos sintéticos con verdad-terreno
tests/test_processing.py, tests/test_bilateral.py, tests/test_insole_ai.py
scripts/                generación de ejemplos, tabla de escenarios y comparativa de suavizado
samples/                dos escaneos sintéticos de ejemplo
docs/                   investigación técnica, parámetros, modo bilateral, plantilla e IA
cli.py                  uso sin interfaz (un pie)
cli_bilateral.py        uso sin interfaz (dos pies)
run.sh, install.sh, Dockerfile, .devcontainer/, .github/workflows/
```

## Pruebas

```bash
./install.sh --dev
.venv/bin/python -m pytest
```

64 pruebas (unos siete minutos): 33 del procesamiento de un pie, 22 del modo bilateral
(descritas en [docs/BILATERAL.md](docs/BILATERAL.md)) y 9 del corte de plantilla, los dedos en el
mapa de presión y la revisión por IA (esta, sin red: con respuestas simuladas de la API). Los escenarios sintéticos tienen una planta analítica
conocida (arco de 16 mm, almohadillas de los dedos, surco metatarsal) con paredes laterales
conectadas, orientación y posición aleatorias, y defectos controlados. Resultados con ~50 000 caras
por modelo en modo `planta` (la fidelidad frente a la planta analítica se mide sin las paredes de
la plantilla; `python scripts/report_scenarios.py`):

| Escenario | Caras | Eliminado (área) | Error RMS vs. analítico | P99,5 | Desv. media / P95 vs. escaneo | Planta preservada | Δ arco | Sólido válido |
|---|---:|---:|---:|---:|---:|---:|---:|:---:|
| limpio (σ=0,05 mm) | 47 742 | 47,3 % | 0,091 mm | 0,38 mm | 0,060 / 0,161 mm | 99,8 % | −0,03 mm | sí |
| fragmentos desconectados | 50 010 | 50,7 % | 0,090 mm | 0,38 mm | 0,059 / 0,160 mm | 99,7 % | −0,03 mm | sí |
| geometría conectada (ala + solapa) | 67 361 | 67,4 % | 0,090 mm | 0,35 mm | 0,059 / 0,160 mm | 99,7 % | 0,00 mm | sí |
| capas superpuestas | 50 753 | 52,0 % | 0,091 mm | 0,37 mm | 0,058 / 0,160 mm | 99,7 % | +0,06 mm | sí |
| ruido (σ=0,3 mm; 0,6 en antepié) | 47 742 | 50,1 % | 0,248 mm | 1,10 mm | 0,229 / 0,594 mm | 88,8 % | +0,08 mm | sí |
| agujeros | 47 464 | 47,7 % | 0,090 mm | 0,38 mm | 0,058 / 0,160 mm | 99,6 % | +0,04 mm | sí |
| geometría incompleta | 39 547 | 42,4 % | 0,102 mm | 0,44 mm | 0,066 / 0,178 mm | 99,1 % | +0,03 mm | sí |
| todos los defectos, en metros (σ=0,2 mm) | 62 333 | 71,1 % | 0,152 mm | 0,56 mm | 0,155 / 0,395 mm | 98,1 % | +0,02 mm | sí |

El suavizado por defecto prioriza una superficie lisa: en un escaneo limpio cede alrededor de
0,05 mm RMS de forma de pequeña amplitud frente al método `local` (0,04 mm de error), que sigue
disponible. Con el **escaneo real** de TrueDepth usado para ajustarlo (243 mm de largo), la
rugosidad local baja de 0,018 a 0,001 mm, la superficie se desplaza 0,19 mm de media (1,0 mm como
máximo) y la profundidad del arco no cambia (22,67 → 22,66 mm).

Además se prueban: lectura/escritura (STL binario y ASCII, OBJ, PLY), el validador con sólidos
defectuosos (abierto, invertido, dos componentes, autointersección, vértice no manifold), el corte
por plano, el relleno biarmónico, la independencia de la orientación, la detección de normales
invertidas, las correcciones manuales, la elección manual de la planta, la eliminación de grano y
puntos aislados conservando el relieve, el método local, los cuatro modos de sólido,
la negativa a exportar un sólido no válido, la invalidación de etapas y la API web de extremo a
extremo.

## Limitaciones conocidas

- **Validación real limitada.** La segmentación está ajustada con modelos sintéticos; el
  suavizado, además, con la superficie ya extraída de un único escaneo real. Con otros escaneos
  puede hacer falta retocar tolerancias (sobre todo *Ángulo máximo*, *Separación de capas*,
  *Distancia de islas* y *Tamaño máx. de irregularidad*); todas son ajustables en la interfaz.
- **Textura grande frente a forma.** El suavizado distingue textura y forma por tamaño y amplitud.
  Una arruga del calcetín más ancha que el *Tamaño máx. de irregularidad* (24 mm) o tan marcada
  como un pliegue de los dedos se conserva; a la inversa, un relieve real suave y de menos de unas
  décimas de milímetro se alisa. Subir o bajar ese tamaño desplaza el equilibrio.
- **Modelo 2,5D.** A partir del remallado la planta es un campo de alturas respecto a la dirección
  plantar: no admite voladizos. Las paredes más inclinadas que el *Ángulo máximo* (62° por defecto)
  no forman parte de la planta. Es lo que garantiza un sólido sin autointersecciones.
- **Geometría espuria coplanar y lisa.** Una superficie unida a la planta sin pliegue ni cambio de
  pendiente (por ejemplo, un cristal de apoyo enrasado con el talón) no se distingue de forma
  automática: hay que quitarla con el pincel o con un plano de corte.
- **Mallas cerradas (pie completo).** Si las normales no indican el lado escaneado, la cara plantar
  se deduce como el lado más plano y se avisa; puede requerir *Invertir lado plantar*.
- **Dedos.** Las almohadillas separadas de la planta por surcos profundos se conservan como
  regiones dudosas si están cerca; los espacios entre dedos más estrechos que el *Cierre del
  contorno* se rellenan por interpolación.
- **Zonas interpoladas.** Los agujeros se rellenan con una superficie suave; el informe indica su
  área y avisa cuando son grandes. No son datos medidos.
- **Borde del contorno.** El primer anillo de vértices toma la altura de la geometría real contigua
  cuando existe y, si no, de una extrapolación plana local: es la zona menos precisa.
- **Detector de autointersecciones.** No detecta triángulos coplanares que se solapan sin compartir
  vértices (caso que la construcción del sólido no puede producir).
- **Un único usuario.** El servidor guarda una sesión en memoria; está pensado para uso local.
- **Docker, Codespaces y la CI** están preparados pero no se han podido ejecutar en el entorno de
  desarrollo; las pruebas y la interfaz se han ejecutado en Linux con Python 3.13, NumPy 2.5,
  SciPy 1.18, Starlette 1.7 y Uvicorn 0.54.

## Licencias de dependencias

NumPy, SciPy, Starlette y Uvicorn son BSD; Matplotlib (solo para los PNG del modo bilateral)
tiene una licencia de tipo BSD. No se usan librerías GPL (PyMeshLab, CGAL, MeshFix).
El proyecto no incluye todavía un archivo de licencia propio: añada el que corresponda.
