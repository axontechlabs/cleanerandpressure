# Investigación técnica y decisiones de diseño

Este documento resume las alternativas estudiadas para cada etapa, por qué se eligió cada
solución y qué se midió para comprobarlo. Las cifras proceden de `scripts/benchmark_smoothing.py`
y `scripts/report_scenarios.py` sobre los modelos sintéticos de `core/synthetic.py`.

## 1. Naturaleza del problema

Un escaneo plantar es una **superficie abierta y fina**, vista esencialmente desde un lado. Tres
propiedades condicionan todo el diseño:

1. No hay interior ni exterior: los métodos volumétricos pierden su principal ventaja.
2. La planta es un **campo de alturas** respecto a la dirección de escaneo, pero no es plana: el
   arco es una concavidad de más de un centímetro que debe conservarse.
3. La geometría espuria puede estar **conectada** a la planta (laterales del pie, soportes,
   láminas de registro defectuoso) o **superpuesta** a ella (capas duplicadas).

## 2. Segmentación y aislamiento de la planta

| Técnica | Qué aporta | Limitación en este caso | Decisión |
|---|---|---|---|
| Componentes conexos | Separa fragmentos flotantes. | No separa nada que esté conectado; "el mayor componente" puede incluir medio pie. | Solo como utilidad (islas, motas). |
| Crecimiento de regiones | Sigue la continuidad de la superficie; conserva el arco porque es continuo con el resto. | Necesita buenos criterios de parada y una semilla fiable. | **Núcleo del algoritmo.** |
| Segmentación por normales | La planta mira hacia el escáner; los laterales, no. | Sensible al ruido; un umbral único deja el borde dentado. | **Usada** con normales suavizadas e histéresis (semilla 35°, crecimiento 62°). |
| Segmentación por curvatura / pliegues | Las láminas espurias suelen unirse por un pliegue. | La curvatura discreta es ruidosa; los surcos de los dedos también son pliegues. | **Usada** como barrera de ángulo diedro alto (80°), no como criterio principal. |
| Topología (aristas no manifold) | Una arista con tres caras delata una lámina pegada a otra. | No todas las uniones espurias son no manifold. | **Usada** como barrera. |
| Agrupamiento espacial (DBSCAN, k-medias) | Agrupa puntos próximos. | Ignora la conectividad y junta capas cercanas, justo lo que hay que separar. | Descartado. |
| RANSAC (planos) | Detecta un suelo o una mesa. | La planta no es un plano y las zonas de apoyo sí lo parecen: riesgo de borrar talón o antepié. | Descartado; la orientación se obtiene de la normal media robusta. |
| Campos de distancia con signo / vóxeles | Robustos frente a topología defectuosa. | Sin interior definido, dos capas a 1,5 mm se funden salvo con vóxeles muy finos. | Descartado en 3D; se usa su análogo 2,5D (z-buffer). |
| Proximidad entre superficies / detección de capas | Identifica duplicados y geometría oculta. | Requiere una dirección de referencia. | **Usada**: rejilla de ocupación y visibilidad en la dirección plantar. |
| Corte de grafo (graph cut) sobre caras | Óptimo global con término de suavidad. | Necesita un solver de flujo máximo y una calibración de costes que solo tiene sentido con datos reales. | Candidato para una versión futura. |

### Algoritmo implementado (`core/segmentation.py`)

En el marco plantar (eje Z = dirección hacia el escáner):

1. **Normales suavizadas** por difusión sobre el grafo de caras, con un radio en milímetros.
2. **Barreras**: aristas no manifold y aristas con ángulo diedro mayor que el umbral.
3. **Visibilidad**: z-buffer de toda la malla; una cara oculta tras otra superficie no es planta.
4. **Semillas**: caras de poca pendiente agrupadas en islas. La principal es la más adelantada
   hacia el escáner entre las de área relevante (o la que el usuario señale con *Elegir planta*).
5. **Crecimiento por ondas con histéresis y rejilla de ocupación**: una cara se acepta si es
   candidata por pendiente, se llega a ella sin cruzar barreras y su celda no está ya ocupada por
   otra altura. Esto último impide que la región salte a una capa superpuesta **aunque esté
   conectada**, y es independiente de la altura absoluta: el arco se acepta porque llega por
   continuidad.
6. **Huella**: lo que cae dentro de la huella de la planta y no pertenece a ella es una capa
   superpuesta o geometría por encima o por debajo, y se elimina.
7. **Islas de extensión**: regiones plantares cercanas y coherentes en altura (dedos separados por
   un surco) se conservan como **dudosas**, se muestran en amarillo y generan un aviso.
8. **Puentes**: los huecos estrechos entre regiones conservadas se cubren con las caras reales
   visibles, no con interpolación.
9. Las **correcciones manuales** prevalecen siempre.

Cada cara eliminada lleva su motivo (pendiente, capa superpuesta, oculta, fragmento lejano…), que
se muestra en el informe para poder ajustar la tolerancia adecuada.

## 3. Reconstrucción de la superficie y del sólido

| Método | Ventajas | Inconvenientes aquí | Decisión |
|---|---|---|---|
| Poisson / Screened Poisson ([Kazhdan y Hoppe, 2013](https://www.hhoppe.com/screenedpoisson.pdf)) | Robusto al ruido; siempre estanco. | Está pensado para objetos cerrados: en una superficie abierta inventa una "tapa", tiende a unir capas finas y a puentear huecos, y suaviza de forma global sin control local. Requiere además normales fiables. | Descartado. |
| SDF + remallado por vóxeles | Garantiza malla manifold. | Memoria cúbica para llegar a décimas de milímetro; funde capas próximas; redondea el contorno. | Descartado en 3D. |
| Reparación directa (tipo MeshFix) | Conserva los vértices originales. | No resuelve capas duplicadas ni densidades dispares; licencia GPL. | Descartado. |
| Relleno de agujeros por triangulación + carenado | Local y fiel. | Requiere triangulación con restricciones y varias pasadas de carenado; frágil con bordes ruidosos. | Sustituido por el relleno sobre rejilla. |
| Offset de malla | Espesor uniforme real. | Se autointerseca en concavidades de radio menor que el espesor (el arco). | Sustituido por desplazamiento a lo largo de la dirección plantar. |
| Operaciones booleanas | Corte exacto con un prisma. | Necesitan una librería robusta (CGAL es GPL para este módulo) y entradas ya estancas. | Innecesarias con campo de alturas. |
| **Campo de alturas (2,5D)** | Exacto para una planta vista de frente; densidad uniforme; relleno y contorno son problemas 2D bien resueltos; el sólido extruido no puede autointersecarse. | No representa voladizos. | **Elegido.** |

### Implementación (`core/surface_remesh.py`, `core/mesh_reconstruction.py`)

- **Alturas**: rasterización de los triángulos originales con interpolación baricéntrica exacta
  (no se interpola entre puntos, se muestrea la superficie medida).
- **Agujeros**: carenado **biarmónico** (mínimos cuadrados del Laplaciano), que continúa la
  curvatura de alrededor en lugar de tender una membrana plana. En la prueba unitaria reproduce
  una superficie cuadrática con error menor que 0,05 mm en un agujero de 12 celdas de diámetro.
- **Contorno**: cierre, relleno, apertura y regularización de la huella; el borde escalonado de la
  rejilla se proyecta a la isolínea de la máscara difuminada y se alisa a lo largo del contorno con
  reproyección (no encoge). Un corte por plano queda recto; el resto, curvo y suave.
- **Altura del borde**: se toma de la geometría real contigua a la selección cuando existe y, si
  no, de un plano local ajustado por mínimos cuadrados. Una extrapolación "plana" levantaba el
  borde hasta 0,9 mm en zonas inclinadas; con este esquema el error RMS del primer anillo baja a
  0,10 mm en el modelo limpio.
- **Sólido**: copia inferior con la misma triangulación (base plana o paralela) y paredes
  verticales subdivididas en filas. Ángulo mínimo de triángulo típico: 15°.

## 4. Suavizado adaptativo

| Método | Comportamiento | Decisión |
|---|---|---|
| Laplaciano | Encoge: aplana el arco y redondea el borde. | Descartado (se usa solo como referencia en la comparativa). |
| Taubin (λ\|μ) | Evita el encogimiento global, pero filtra igual en todas partes y no distingue relieve de ruido. | Descartado. |
| HC (Vollmer et al.) | Laplaciano con corrección que devuelve el desplazamiento medio. | Su idea se reutiliza: se anula el desplazamiento de baja frecuencia en las zonas cóncavas. |
| Bilateral sobre vértices (Fleishman et al.; Jones et al.) | Preserva aristas vivas usando un núcleo de rango ([Jones, Durand y Desbrun, 2003](https://people.csail.mit.edu/thouis/JDD03.pdf)). | Se adopta el **peso de rango** como segunda pasada robusta. |
| Bilateral sobre normales ([Zheng et al., 2011](https://hongbofu.people.ust.hk/doc/bilateral_normal_filtering_tvcg11.pdf)) | El mejor en piezas con aristas vivas; dos fases (normales, luego vértices). | Un pie no tiene aristas vivas; su ventaja no compensa la complejidad. |
| Difusión anisótropa | Preserva rasgos según la curvatura. | Sensible a parámetros y cara de iterar. | Descartada. |
| Regresión local cuadrática robusta (MLS) | Reproduce exactamente la curvatura suave, por lo que no encoge; el residuo del ajuste es una medida directa del ruido local. Con un radio fijo no puede quitar la textura de varios milímetros sin tocar los dedos. | Disponible como método `local`. |
| **Descomposición multiescala con *splines* de placa delgada** | Separa la superficie por tamaño de detalle y decide en cada tamaño y en cada zona si es textura o relieve, con umbrales medidos en el propio escaneo. | **Elegida por defecto.** |

La comparación publicada por [Beşdok y Çivicioğlu (2023)](https://doi.org/10.53093/mephoj.1349021)
entre filtros bilateral, Laplaciano y de Taubin sitúa al de Taubin como término medio entre la
preservación de detalle del bilateral y la suavidad del Laplaciano; aquí el criterio decisivo es
otro, no aplanar una concavidad grande y suave, y por eso se optó primero por un ajuste local de
segundo orden.

**Por qué se cambió el método por defecto.** El primer escaneo real (iPhone TrueDepth, aristas de
~2 mm) mostró que la textura que molesta no es solo grano fino: son bultos y vetas de 5 a 20 mm con
amplitudes de 0,05 a 0,2 mm. Un filtro de radio fijo capaz de quitarlos (corte de ~22 mm) emborrona
los dedos; uno que respeta los dedos (corte de ~8 mm) deja la textura. Hace falta decidir por
tamaño *y* por zona.

### Implementación (`core/smoothing.py`)

**Método multiescala (por defecto).**

1. *Paso bajo sin sesgo.* Cada nivel es un *spline* de placa delgada, min Σ(f − z)² + λ∫(Δf)², con
   λ = (corte/2π)⁴. La respuesta cae con la cuarta potencia de la frecuencia y reproduce
   exactamente planos y curvaturas constantes: el arco no se aplana. Laplaciano de cotangentes
   sobre la proyección, sistema disperso resuelto por factorización directa.
2. *Grano y puntos aislados.* El nivel 0 (corte de 4 mm) se calcula con pesos robustos de Cauchy:
   un punto aislado no arrastra a la superficie. Todo lo que queda por debajo se elimina.
3. *Bandas.* Cortes en progresión geométrica (4, 6, 8, 12, 18, 24 mm). En cada banda se calcula la
   energía local y su mediana, que es el nivel típico de textura de ese escaneo en ese tamaño.
4. *Modelo de textura del escaneo.* La forma anatómica apenas tiene energía en las bandas finas,
   así que ahí se mide la textura: su pendiente típica (amplitud/tamaño). Se supone que en tamaños
   mayores la textura no supera esa pendiente; lo que la excede es forma. Un escaneo rugoso da una
   pendiente alta y se alisa por completo hasta el tamaño máximo; uno limpio casi no se toca.
5. *Relieve frente a textura.* Una zona se conserva en una banda si su energía supera con
   claridad el nivel de textura y forma un núcleo extenso (umbral con histéresis y área mínima):
   dedos, surcos y bordes sí; hoyuelos y granos aislados no. Donde una banda gruesa es relieve, la
   siguiente necesita menos evidencia, porque un pliegue real tiene energía en todas las escalas.
6. *Contorno.* Un *spline* de borde libre anula la curvatura en el borde y endereza la caída del
   canto. Junto al contorno las bandas no se eliminan: se promedian a lo largo del borde, lo que
   conserva el perfil del canto y quita los bultos.
7. *Tolerancia de desviación* respecto a la superficie sin grano, con saturación suave.

**Método local.** Para cada vértice se ajusta un polinomio cuadrático a sus vecinos dentro del
radio de influencia; la intervención es `ruido² / (ruido² + umbral²)`, se reduce donde la curvatura
coherente es alta y en las zonas cóncavas, y hay un límite estricto de desviación por vértice.

### Comparativa medida

Error respecto a la planta analítica, partiendo de la misma superficie recortada (≈ 48 000 caras de
entrada, malla de 1 mm). "Sesgo arco" positivo significa que el arco se rellena; "rugosidad" es la
mediana del residuo respecto a una media local (menor = más lisa).

| Escenario | Método | RMS (mm) | Sesgo arco (mm) | RMS dedos (mm) | Rugosidad (mm) |
|---|---|---:|---:|---:|---:|
| limpio | sin suavizar | 0,044 | −0,007 | 0,054 | 0,047 |
| limpio | Laplaciano, 10 iteraciones | 0,379 | +0,039 | 0,522 | 0,034 |
| limpio | Taubin, 20 iteraciones | 0,159 | +0,002 | 0,239 | 0,036 |
| limpio | MLS local | 0,039 | −0,007 | 0,071 | 0,041 |
| limpio | **Multiescala (24 mm)** | 0,091 | +0,013 | 0,149 | 0,041 |
| limpio | Multiescala, 16 mm | 0,071 | +0,011 | 0,153 | 0,040 |
| ruido σ=0,3–0,6 | sin suavizar | 0,374 | +0,023 | 0,462 | 0,180 |
| ruido σ=0,3–0,6 | Laplaciano, 10 iteraciones | 0,472 | +0,071 | 0,633 | 0,050 |
| ruido σ=0,3–0,6 | Taubin, 20 iteraciones | 0,309 | +0,031 | 0,378 | 0,090 |
| ruido σ=0,3–0,6 | MLS local | 0,224 | +0,021 | 0,280 | 0,083 |
| ruido σ=0,3–0,6 | **Multiescala (24 mm)** | 0,248 | +0,014 | 0,423 | 0,050 |
| ruido σ=0,3–0,6 | Multiescala, 16 mm | 0,193 | +0,037 | 0,357 | 0,049 |
| todos los defectos | sin suavizar | 0,191 | −0,004 | 0,196 | 0,107 |
| todos los defectos | Laplaciano, 10 iteraciones | 0,357 | +0,038 | 0,271 | 0,036 |
| todos los defectos | Taubin, 20 iteraciones | 0,192 | +0,003 | 0,162 | 0,057 |
| todos los defectos | MLS local | 0,107 | −0,005 | 0,111 | 0,054 |
| todos los defectos | **Multiescala (24 mm)** | 0,152 | −0,019 | 0,242 | 0,039 |
| todos los defectos | Multiescala, 16 mm | 0,094 | +0,009 | 0,170 | 0,038 |

Lectura honesta de la tabla: el método multiescala deja la superficie tan lisa como un Laplaciano
sin su deformación (0,09–0,25 mm de error frente a 0,36–0,47 mm), pero **no** es el de menor error
frente a la forma analítica: el MLS local lo es en el modelo limpio. En estos modelos sintéticos
la banda de 16–24 mm contiene sobre todo forma real (el hombro del contorno, el surco metatarsal)
y con ruido fuerte ese surco, de 1,2 mm, queda atenuado. Es el precio de un alisado total, y por
eso el tamaño máximo es ajustable: con 16 mm el error baja y la rugosidad es la misma.

En el escaneo real, en cambio, con 16 mm aún se aprecian ondulaciones al exagerar el sombreado y
con 24 mm desaparecen; el valor por defecto se fijó con ese escaneo. Resultados sobre él
(superficie de 243 × 84 mm): rugosidad local 0,018 → 0,001 mm, desplazamiento medio 0,19 mm y
máximo 1,0 mm, profundidad del arco 22,67 → 22,66 mm, dedos conservados.

**Limitación de esta validación.** El escaneo real disponible era la salida del programa con el
suavizado anterior, no el STL en bruto. Para comprobar el comportamiento con grano se le añadió
ruido simulado (correlacionado, con puntos aislados y un patrón de tejido): el resultado es
igualmente liso, pero el espectro real del sensor y de un calcetín puede diferir del simulado.

## 5. Librerías

| Librería | Licencia | Valoración |
|---|---|---|
| NumPy, SciPy | BSD | Suficientes para todo el núcleo: álgebra dispersa, k-d tree, morfología, envolvente convexa. **Elegidas.** |
| Trimesh | MIT | Muy cómoda para E/S y consultas, pero las operaciones clave (booleanas, reparación) delegan en motores externos. Prescindible. |
| Open3D | MIT | Poisson, remallado por vóxeles y Taubin; ninguno es el método adecuado aquí. Paquete pesado y dependiente de la versión de Python. |
| PyMeshLab | GPL v3 | Catálogo enorme de filtros; la licencia condiciona la distribución del producto. |
| CGAL (Polygon Mesh Processing) | GPL / comercial | Referencia en robustez; licencia y compilación complejas. |
| libigl | MPL 2.0 en su núcleo; los módulos que enlazan con CGAL heredan copyleft ([licencia](https://libigl.github.io/license)) | Buena opción futura para operadores cotangente o booleanas. |
| MeshFix / pymeshfix | GPL v3 | Repara agujeros y autointersecciones, pero no resuelve capas ni segmentación. |
| PyVista / VTK | MIT / BSD | Excelente visualización de escritorio; el pincel sobre caras en web requiere igualmente un visor propio. |
| Blender (bpy) | GPL | Excesivo como dependencia. |
| Streamlit / Gradio | Apache 2.0 | No permiten pintar caras ni mover un plano de corte en vivo sobre el modelo. |
| FastAPI | MIT | Equivalente a Starlette (sobre la que está construida) más validación que aquí no se necesita. |
| **Starlette + Uvicorn** | BSD | Servidor mínimo y estable. **Elegidos.** |
| Three.js | MIT | Estándar para WebGL; exige incluir la librería o depender de un CDN. |
| **Visor WebGL2 propio** | — | 350 líneas, sin dependencias ni conexión; selección de caras por búfer de identificadores, que hace natural un pincel que solo pinta lo visible. **Elegido.** |

Resultado: cuatro dependencias con ruedas precompiladas para macOS (Intel y Apple Silicon), todas
con licencia permisiva. Los algoritmos están vectorizados; un escaneo de 515 000 caras se procesa
completo en menos de un minuto. El suavizado multiescala tarda unos 3 s con la malla de 1 mm
(~25 000 vértices), 10 s a 0,5 mm y 23 s a 0,35 mm (130 000 vértices, 1,4 GB de memoria).

## 6. Arquitectura

```
navegador (visor WebGL2, herramientas) ── HTTP local ── app/server.py ── core/pipeline.py ── core/*
```

- `core/` no conoce la interfaz: se usa igual desde la web, la línea de comandos y las pruebas.
- `Pipeline` guarda las etapas (original → segmentado → recortado → suavizado → sólido); rehacer
  una etapa invalida las posteriores y conserva las anteriores.
- El servidor mantiene una única sesión en memoria y serializa las operaciones (uso local).
- Las mallas viajan en binario (Float32/Uint32) y las etiquetas como un byte por cara.

## 7. Referencias

- G. Taubin, *A signal processing approach to fair surface design*, SIGGRAPH 1995.
- J. Vollmer, R. Mencl, H. Müller, *Improved Laplacian smoothing of noisy surface meshes*, Eurographics 1999.
- S. Fleishman, I. Drori, D. Cohen-Or, *Bilateral mesh denoising*, SIGGRAPH 2003.
- T. Jones, F. Durand, M. Desbrun, *Non-iterative, feature-preserving mesh smoothing*, SIGGRAPH 2003. <https://people.csail.mit.edu/thouis/JDD03.pdf>
- Y. Zheng, H. Fu, O. K.-C. Au, C.-L. Tai, *Bilateral normal filtering for mesh denoising*, IEEE TVCG 2011. <https://hongbofu.people.ust.hk/doc/bilateral_normal_filtering_tvcg11.pdf>
- E. Beşdok, P. Çivicioğlu, *Analysing detail preserving capabilities of bilateral, Laplacian and Taubin mesh filtering methods*, Mersin Photogrammetry Journal 2023. <https://doi.org/10.53093/mephoj.1349021>
- M. Kazhdan, H. Hoppe, *Screened Poisson surface reconstruction*, ACM TOG 2013. <https://www.hhoppe.com/screenedpoisson.pdf>
- D. Levin, *Mesh-independent surface interpolation*, 2003 (mínimos cuadrados móviles).
- P. Liepa, *Filling holes in meshes*, SGP 2003.
- M. Botsch, L. Kobbelt, *A remeshing approach to multiresolution modeling*, SGP 2004.
- Licencia de libigl: <https://libigl.github.io/license>
