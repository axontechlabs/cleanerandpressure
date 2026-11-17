# Modo bilateral: simulación de bipedestación estática con los dos pies

Página `http://127.0.0.1:8000/bilateral` (botón **Modo bilateral (dos pies) →** en la aplicación de un
pie). La aplicación de un pie no cambia; este modo es una función añadida con su propia sesión.

## Flujo

1. **Escaneos.** Un STL para el pie izquierdo y otro para el derecho, en campos separados.
2. **Limpieza.** *Procesar ambos escaneos* ejecuta, por separado, el mismo procesamiento automático
   de la aplicación de un pie (segmentación, contorno, suavizado, sólido y validación). Resultado:
   dos STL limpios e independientes, que se descargan en el marco de su archivo original. Si alguno
   no supera la validación, no se exporta y no se puede simular.
3. **Persona.** Peso corporal (kg), estatura (cm, opcional) y reparto izquierdo/derecho (50/50
   por defecto), o bien la proyección del centro de masas si se conoce.
4. **Postura.** *Colocar pies y ver en 3D* coloca ambos pies en el suelo común.
5. **Simulación.** *Simular bipedestación bilateral* resuelve la carga de los dos pies a la vez y
   muestra el mapa bilateral, los mapas individuales, el 3D coloreado y el informe.

Exportaciones: los dos STL limpios (también OBJ/PLY), el mapa bilateral y los dos individuales en
PNG, el CSV de presiones (una fila por celda de 1 mm² de cada huella) y el JSON con todos los
resultados.

## Preparación de la geometría

**Lado del pie.** Se deduce de la geometría y se compara con el campo en que se cargó el archivo:

- el eje talón-dedos es el eje principal de la huella, con el antepié en el extremo más ancho;
- el lado medial es el del arco (en el mediopié, el borde interno queda más alejado del suelo);
- con el eje, el lado medial y la vertical, un pie derecho y uno izquierdo son imágenes especulares y
  no se pueden confundir por un giro.

Si no coincide con lo declarado, se avisa y se ofrece *Intercambiar pies*. La geometría nunca se
refleja para "arreglarlo".

**Orientación respecto al suelo.** No se supone que la planta sea plana ni que el escaneo esté
horizontal. Se busca el plano sobre el que apoyaría un pie rígido cargado en su línea de carga: la
cara de la envolvente convexa de la planta que contiene la proyección de ese punto (normalmente el
trípode talón – cabezas metatarsianas). Después, el equilibrio de momentos ajusta el cabeceo y el
alabeo de cada pie (ver más abajo).

**Postura y separación.** Por defecto se usa la postura media preferida en bipedestación tranquila
medida por McIlroy y Maki (1997) en 262 personas: **17 cm entre centros de talón y 14° entre los
ejes largos de los pies** (7° de apertura por pie). La separación preferida aumenta con la estatura
(r = 0,56 en adultos de 1,69 ± 0,08 m, con 17,7 ± 4,7 cm de separación). Si se indica la estatura,
la separación se estima con la recta de regresión que dan esos datos:

    separación (mm) = 170 + 3,3 · (estatura (cm) − 169)

Queda limitada a 10–28 cm. La correlación es moderada: es una estimación mejor que la media, no la
postura de la persona. Separación, apertura de cada pie y avance de uno respecto al otro siguen
siendo ajustables, y una separación escrita a mano tiene prioridad. La posición natural de una
persona concreta no se puede deducir de dos escaneos independientes, porque no contienen la
relación entre los pies. Si con los valores indicados las huellas se solapan o quedan a menos de
8 mm, la separación se aumenta automáticamente y se avisa.

**Qué cambia con la estatura.** En el modo *reparto I/D indicado*, cada pie se equilibra con su
propia línea de carga, así que la separación no cambia el mapa de cada pie. Solo mueve el centro de
presiones global. Donde sí cuenta es en el modo *centro de masas*, porque reparte el peso entre los
pies según la posición del centro de masas respecto a ellos.

**Comprobación de escala.** La estatura también sirve para comprobar que la escala del escaneo es
coherente. Sin carga, el pie mide el 14,6–15,1 % de la estatura (Xiong et al., 2009) y suele quedar
entre el 14 y el 16 %. Si un pie queda fuera del 13–17,5 %, se avisa: lo más probable es que el
escaneo esté en otras unidades o que la estatura sea errónea.

Las transformaciones de colocación se aplican solo a la copia usada en la simulación; los STL
limpios no cambian (lo comprueba una prueba automática).

## Modelo biomecánico

El pie se modela con varios segmentos y se carga en este orden, como ocurre al ponerse de pie:

1. **Asentamiento por gravedad.** El pie, como sólido rígido cargado en su línea de carga, cae
   sobre el suelo y queda apoyado en el plano de su envolvente convexa que contiene esa línea
   (normalmente talón y cabezas metatarsianas). El informe indica qué regiones tocan el suelo en
   este momento, antes de comprimir el tejido.
2. **Dedos.** En un escaneo sin carga los dedos suelen quedar algo levantados (en extensión), pero
   en bipedestación normalmente apoyan los cinco y llevan algo de carga. El modelo lo reproduce así:
   - **Localización.** Por franjas mediolaterales de 4 mm se busca la almohadilla de las cabezas
     metatarsianas y, delante, el surco digital (o, si los dedos están levantados, el punto donde el
     perfil deja de estar apoyado). Con eso se ajusta el eje de las articulaciones
     metatarsofalángicas (MTF) como una parábola (la 1.ª MTF más adelantada que la 5.ª). Los dedos
     empiezan en el surco, unos 17 mm (7 % de la longitud) por delante del eje.
   - **Cinco dedos.** La fila de los dedos se divide en cinco con las proporciones anatómicas
     (hallux ≈ 31 % de la anchura). Cada límite se ajusta al hueco interdigital más cercano que
     muestre la geometría. Donde no hay hueco visible, la transición entre dedos vecinos es suave
     (piel continua).
   - **Asentamiento.** Cada dedo es un segmento rígido que gira en su articulación MTF (eje a 20 mm
     sobre la piel: unos 10 mm de tejido más el radio de la cabeza metatarsiana) hasta que su pulpejo
     toca el suelo. El giro máximo es de 50° desde la postura del escaneo. Si un dedo necesita más,
     no apoya y se avisa (dedo en garra o muy levantado). Si alguno necesita más de 30°, también se
     avisa.
   - **Carga.** Bajo carga, los dedos se aprietan contra el suelo con un momento flexor alrededor de
     las MTF: tono de los flexores más el mecanismo de molinete inverso de la fascia plantar, que
     flexiona los dedos al cargar el arco. Por defecto son 700 N·mm por pie con medio peso,
     proporcional a la carga del pie. Hay dos grupos musculares: el hallux (flexor largo del hallux,
     40 %) y los dedos 2.º–5.º (flexores largo y corto de los dedos y fascia, 60 %). El apoyo de cada
     grupo se resuelve para que el momento de la reacción del suelo iguale el momento flexor. El
     reparto entre los dedos de un grupo sale de su geometría. El dedo no clava la punta: sus
     articulaciones interfalángicas y el pulpejo blando hacen que se aplaste contra el suelo, y el
     pulpejo no se comprime más del 59 % (más allá, el dedo se flexionaría). Si por eso un grupo no
     alcanza el 90 % de su momento, se avisa.
3. **Arco interno.** Se mide en el escaneo sin carga, con el pie apoyado en su plano:
   - **Altura del arco:** la del borde medial de la planta (tercio medial de la anchura) en su punto
     más alto del mediopié, en mm y en % de la longitud del pie.
   - **Ápice:** dónde está ese punto (bajo el navicular), en % de la longitud desde el centro del
     talón.
   - **Holgura lateral:** la altura mínima del borde lateral del mediopié (cuboides, base del 5.º
     metatarsiano). Si es de pocos mm, ese borde apoya enseguida y el mediopié carga más. Es el caso
     típico de un pie normal con banda lateral de apoyo.

   **Descenso del arco con la carga.** La caída del navicular en adultos sanos es de unos 3,6–8,1 mm
   de media y crece con la longitud del pie: 0,31–0,40 mm por cm (Nielsen et al., 2009). Por
   defecto se usan 6 mm para un pie de 25,3 cm, más 0,35 mm por cm de diferencia, con medio peso
   por pie y proporcional a la carga. Si se ha medido la caída del navicular de la persona, se
   escribe y tiene prioridad.

   **Dónde se aplica.** El descenso sigue la forma del arco medido: es nulo en el apoyo del talón,
   máximo en el ápice y nulo de nuevo en la 1.ª cabeza metatarsiana. En la columna lateral, mucho
   más rígida, también es nulo. Antes el máximo se colocaba en una posición fija (45 %); en los
   escaneos reales probados el ápice estaba hacia el 34–36 %. El descenso solo cierra la holgura
   hasta el suelo: el esqueleto no empuja el tejido por sí mismo. Si el escaneo se tomó de pie, se
   marca *Escaneo tomado en carga* y no se aplica.

   **Lo que la altura del arco no dice.** No dice cuánto baja el arco. Los pies cavos suelen ser
   rígidos y los planos, flexibles (Zifchock et al., 2017), pero la relación no es lo bastante
   estrecha para deducir el descenso de la altura. Por eso la altura se mide y se usa para la
   forma y la posición del descenso, y su magnitud viene de la longitud del pie o de la medida de
   la persona.

   **Tipo de huella.** Con la huella simulada se calcula el índice del arco de Cavanagh y Rodgers.
   Se clasifica como arco alto (≤ 0,21), normal (0,21–0,26) o bajo (≥ 0,26).

4. **Tejido blando y equilibrio.** El suelo comprime el tejido plantar de cada región según una ley
   exponencial con acoplamiento lateral:

       p = E₀ / β · (exp(β · δ / t) − 1)

   - δ: compresión local (mm);
   - t: espesor del tejido (mm);
   - E₀: módulo inicial (kPa);
   - β: no linealidad (la curva se rigidiza hacia una deformación de 1/β).

Valores por defecto (editables por región y por pie):

| Región | t (mm) | E₀ (kPa) | β |
|---|---:|---:|---:|
| Talón medial / lateral | 19 | 35 | 8 |
| Mediopié | 12 | 30 | 6 |
| Antepié 1.er MTT | 10 | 35 | 6 |
| Antepié 2.º–3.er MTT | 11 | 35 | 6 |
| Antepié 4.º–5.º MTT | 9 | 35 | 6 |
| Hallux | 8 | 30 | 6 |
| Dedos 2.º–5.º | 7 | 30 | 6 |

**Cuánto se deforma el tejido.** La simulación se ha comprobado frente a medidas de compresión del
tejido plantar:

- **Talón.** El índice de compresibilidad de la almohadilla del talón (espesor cargado / sin
  cargar) es 0,53 ± 0,09 en 400 personas sanas (Prichasuk, 1994) y 0,53 en adultos jóvenes (Hsu et
  al., 1998). Es decir, el talón se comprime un ~47 % bajo el peso. El modelo da un 46–47 %.
- **Antepié.** Bajo la 2.ª cabeza metatarsiana el tejido se comprime de media un 46 % en el momento
  de mayor carga de la marcha (Cavanagh, 1999), con presiones muy superiores a las de estar de pie.
  El modelo da un 46–49 % en bipedestación, en el límite blando del rango.
- **Conclusión.** El tejido no estaba demasiado rígido. Ablandarlo más contradiría esas medidas. Lo
  que faltaba era la deformación articular: los dedos giran en sus MTF hasta apoyar y se aprietan
  contra el suelo.
- **Presiones de los dedos.** En bipedestación, la presión bajo el hallux y el 2.º dedo es de
  alrededor del 20 % de la presión máxima del pie, y bajo los dedos 3.º–5.º de alrededor del 15 %
  (Gawronska y Lorkowski, 2021). Con los valores por defecto los dedos llevan en torno al 5–7 % de
  la carga del pie, con picos del 20–30 % del máximo.

- **Espesores:** siguen las medidas ecográficas publicadas (almohadilla del talón ~19–20 mm; bajo las
  cabezas metatarsianas ~7–15 mm según el estudio).
- **β:** sigue las medidas de indentación in vivo. El tejido entra en régimen no lineal hacia el
  12 % de su espesor en el talón y el 16 % bajo la 2.ª cabeza metatarsiana: el talón se rigidiza
  antes (β ≈ 8) que el antepié (β ≈ 6).
- **E₀:** **calibrado**, no medido. Junto con la línea de carga (32 %), está ajustado para que un pie
  normal reproduzca el reparto clásico en bipedestación descalza de Cavanagh et al. (1987): talón
  ~60 %, mediopié ~8 %, antepié ~28 %, dedos pocos %, y pico de presión en el talón.

### Equilibrio

**Fuerzas (siempre, exacto).** `F_izq + F_der = m · 9,80665`. Nunca se aplica el peso completo a
cada pie. El error de equilibrio aparece en el informe (en las pruebas, menor que 10⁻⁶ del peso).

**Momentos de cada pie (siempre).** Cada pie tiene cuatro incógnitas: aproximación vertical,
cabeceo, alabeo y giro de los dedos. Tiene también cuatro ecuaciones: su fuerza, los dos momentos
que sitúan su centro de presiones bajo la **línea de carga** de la pierna y el equilibrio de
momentos de los dedos alrededor de las articulaciones MTF. El problema está determinado. Por defecto la línea
de carga cae al 32 % de la longitud del pie medido desde el centro del talón (≈ 38–39 % desde su
borde posterior), en la línea media de la huella, porque en bipedestación tranquila el centro de
presiones queda justo por delante de los tobillos. Medido desde el borde posterior del talón, en
reposo cae entre el 31 y el 58 % de la longitud del pie según la persona, con el 44,5 % de media
con los pies separados. El informe da las dos medidas. Con el
reparto de Cavanagh (talón 60 %, mediopié 8 %, antepié 28 %, dedos 4 %) el centro de presiones
cae hacia el 33–35 %. Es ajustable.

**Reparto izquierdo/derecho.** No se deduce de la forma de los pies: sin el centro de masas o una
medida externa no es identificable. Hay dos modos:

- *Reparto I/D indicado* (por defecto 50/50). El reparto es una hipótesis del usuario. El centro de
  presiones global resultante es el punto en que **debería** estar el centro de masas para que esa
  hipótesis estuviera en equilibrio; el informe lo muestra.
- *Centro de masas conocido*. Se introduce la proyección horizontal del centro de masas (lateral en
  mm y anteroposterior en % de la longitud del pie). Entonces el equilibrio de momentos del cuerpo
  entero (centro de presiones global = centro de masas) determina el reparto I/D y la posición
  anteroposterior común de las líneas de carga (inclinación del cuerpo). Si el punto cae fuera de
  la base de apoyo no hay equilibrio y se dice.

### Qué información haría falta para estimar el centro de presiones global

El reparto y el centro de presiones global dependen de dónde está el centro de masas, que no está en
los escaneos. Formas de obtenerlo, de más a menos precisa:

1. Una plataforma de fuerzas (o dos básculas, una bajo cada pie): da directamente el reparto I/D y,
   con plataforma, el centro de presiones.
2. Fotogrametría o captura de movimiento con un modelo de segmentos corporales: estima el centro de
   masas a partir de la postura.
3. Una foto frontal y otra lateral con plomada respecto a los pies: aproximación clínica del
   desplazamiento del centro de masas.

Con el dato 1 se usa el modo de reparto indicado; con 2 o 3, el modo de centro de masas.

## Resultados

- **Conjunto**: peso, fuerza gravitatoria, fuerza soportada, reparto I/D y su origen (hipótesis o
  equilibrio), superficie de contacto total, presión media y máxima global, centro de presiones
  global, error de equilibrio de fuerzas (y de momentos en el modo de centro de masas).
- **Cada pie**:
  - fuerza y % del peso, superficie de contacto, presión media y máxima;
  - centro de presiones (y su posición en % de la longitud), retropié/antepié;
  - índice del arco de Cavanagh, compresión máxima del tejido y giro del pie;
  - tabla por región: % del pie, % del peso, superficie y presión máxima.
- **Comparación**: cada indicador izquierdo, derecho, diferencia y asimetría
  `2·|I − D| / (I + D)`, resaltando las mayores del 10 %.

El contacto se cuenta donde la presión supera el umbral (2 kPa por defecto, como el de un sensor).
La presión media es fuerza / superficie de contacto.

**Mapas.** Fondo negro, gradiente azul → cian → verde → amarillo → naranja → rojo, una sola escala
absoluta en kPa para los dos pies y para los mapas individuales (no se normaliza cada pie), una
sola leyenda. Muestran la huella completa de cada pie (contorno gris), el centro de presiones de
cada pie (+) y el global (○). Vista superior: pie izquierdo a la izquierda, dedos hacia arriba.

## Línea de comandos

```bash
.venv/bin/python cli_bilateral.py izquierdo.stl derecho.stl --peso 70 -o resultados/
.venv/bin/python cli_bilateral.py izq.stl der.stl --peso 82 --reparto-izquierdo 45 -o salida/
.venv/bin/python cli_bilateral.py izq.stl der.stl --peso 70 --centro-masas 15 38 -o salida/
.venv/bin/python cli_bilateral.py izq.stl der.stl --peso 70 --estatura 165 --caida-navicular 7 -o salida/
```

## Pruebas (`tests/test_bilateral.py`)

Pareja sintética con arcos distintos (16 y 22 mm) y el derecho obtenido por simetría, cada uno con
su propia orientación aleatoria. Se comprueba:

- dos sólidos válidos e independientes;
- la detección del lado y el aviso si los archivos están cruzados;
- el equilibrio exacto de fuerzas con repartos 50/50, 45/55 y 62/38;
- el equilibrio de momentos de cada pie (centro de presiones a menos de 0,5 mm de la línea de carga);
- que los dedos apoyan (más de 2 cm²) con carga ligera (menos del 10 %) y que sin momento flexor
  cargan menos;
- con escaneos de dedos en extensión (12°) con pulpejos: que los cinco dedos apoyan y cargan, que
  el hallux es el que más carga, que los dedos llevan el 3–10 % del pie y que se cumple el
  equilibrio de momentos de cada grupo alrededor de las MTF (error < 1 %);
- que la compresión del talón queda en el 35–56 % (índice de compresibilidad publicado) y la
  máxima por debajo del 60 %;
- que el momento flexor gobierna la carga de los dedos (sin él, no cargan);
- con dedos muy levantados (30°): que siguen apoyando los cinco y se avisa; y que, si el giro
  máximo permitido no basta, no apoyan y se avisa;
- el reparto de referencia: talón 50–72 %, mediopié < 15 %, antepié 20–40 %, pico en el talón;
- el descenso del arco y su desactivación para escaneos en carga; que el descenso por defecto
  sigue la longitud del pie y que un valor indicado tiene prioridad;
- que el arco se mide en el escaneo (el derecho sintético, más alto, mide más) y su ápice cae en
  una posición anatómica;
- que la estatura fija la separación (salvo que se indique otra) y que una estatura incoherente con
  la longitud de los pies da un aviso;
- rangos fisiológicos (pico entre 50 y 250 kPa, contacto entre 60 y 180 cm² por pie, retropié por
  encima del 30 %);
- que pies distintos dan mapas distintos y que las regiones suman la fuerza del pie;
- el modo de centro de masas, incluido el caso sin equilibrio posible;
- que no hay solapes y que los STL limpios no cambian al colocar los pies;
- las exportaciones, que las propiedades de un pie no afectan al otro y la API web.

Con la pareja sintética de dedos en extensión (12°) con pulpejos, 70 kg y 50/50:

| Indicador | Antes | Ahora |
|---|---:|---:|
| Dedos que apoyan | 0 | 5 |
| Carga en los dedos (% del pie) | 0 % | 6,8 % (hallux 2,6 %) |
| Contacto bajo los dedos | 0 cm² | 15 cm² |
| Pico hallux / dedos 2.º–5.º | 5 / 5 kPa | 50 / 32 kPa |
| Talón / mediopié / antepié | 58 / 9 / 33 % | 60 / 8 / 26 % |
| Compresión del talón | 46 % | 46 % |

Las cifras del escaneo real que figuraban aquí se obtuvieron con el modelo anterior y hay que
recalcularlas.

Son comprobaciones de verosimilitud frente a la literatura, no una validación.

## Limitaciones

- **Sin validar frente a mediciones.** Ninguna presión simulada se ha comparado con una plataforma.
  Los módulos del tejido son un valor de partida; para usar las cifras absolutas haría falta
  calibrarlos con al menos una medida baropodométrica de la misma persona.
- **Arco.** La forma y la posición del descenso salen del escaneo, pero su magnitud es la típica
  para la longitud del pie, no la rigidez de ese pie. Un pie muy flexible o muy rígido necesita la
  medida de la caída del navicular. Si el escaneo se hizo en carga, hay que desactivarlo.
- **Estatura.** Solo estima la separación entre los pies (correlación moderada) y comprueba la
  escala. No cambia el mapa de cada pie en el modo de reparto indicado.
- **Dedos.** Los cinco dedos se separan por proporciones anatómicas ajustadas a los huecos
  interdigitales visibles, no por segmentación de cada falange. El momento flexor (700 N·mm) está
  calibrado para dar la carga ligera publicada, no medido en la persona. Las articulaciones
  interfalángicas no se modelan por separado. En dedos en garra, en martillo o con hallux limitus,
  el resultado de los dedos es poco fiable (se avisa cuando el giro necesario es grande). El "dedo
  flotante" (algún dedo que no toca el suelo de pie) es frecuente en algunas poblaciones. Para
  simularlo, baje el giro máximo o el momento flexor.
- **Postura.** La separación y la apertura por defecto son la media de una población, no las de la
  persona.
- **Línea de carga.** Su posición (32 %) es una hipótesis razonable para bipedestación tranquila, no
  una medida; cambia el reparto talón/antepié.
- **Regiones.** La máscara porcentual no localiza cada cabeza metatarsiana ni cada dedo
  individualmente.
- **Estática.** No hay balanceo postural, ni marcha, ni calzado.
- **Resultados de una simulación.** No sustituyen a una medida clínica; los mapas lo indican.

## Referencias

- W. E. McIlroy, B. E. Maki. *Preferred placement of the feet during quiet stance: development of a
  standardized foot placement for balance testing*. Clinical Biomechanics 12 (1997) 66–70.
- P. R. Cavanagh, M. M. Rodgers, A. Iiboshi. *Pressure distribution under symptom-free feet during
  barefoot standing*. Foot & Ankle 7 (1987) 262–276.
- P. R. Cavanagh, M. M. Rodgers. *The arch index: a useful measure from footprints*. J. Biomech. 20
  (1987) 547–551.
- P. R. Cavanagh. *Plantar soft tissue thickness during ground contact in walking*. J. Biomech. 32
  (1999) 623–628: compresión media máxima del 45,7 % bajo la 2.ª cabeza metatarsiana.
- J. C. Teoh, B. Lim, T. Lee. *Effect of deformation depth on plantar soft tissue behavior*. J. Foot
  Ankle Res. 7 (Suppl. 1) A84 (2014): umbral no lineal del 12 % (talón) y el 16 % (2.ª cabeza
  metatarsiana).
- R. G. Nielsen et al. *Determination of normal values for navicular drop during walking: a new
  model correcting for foot length and gender*. J. Foot Ankle Res. 2 (2009) 12: caída del navicular
  de 3,6–8,1 mm de media en adultos sanos; +0,40 mm (hombres) y +0,31 mm (mujeres) por cm de
  longitud del pie.
- *Influence of stance width on standing balance in healthy older adults*. J. Neurol. 269 (2022)
  6228–6236, <https://doi.org/10.1007/s00415-022-11144-5>: separación preferida 17,7 ± 4,7 cm,
  correlación con la estatura r = 0,56.
- S. Xiong et al. *Foot deformations under different load-bearing conditions and their
  relationships to stature and body weight*. Anthropol. Sci. 117 (2009) 77–88: longitud del pie del
  14,6–15,1 % de la estatura.
- R. A. Zifchock et al. *The relationship between arch height and arch flexibility*. J. Am. Podiatr.
  Med. Assoc. 107 (2017) 119–123: los pies cavos tienden a ser rígidos y los planos flexibles.
- *Relationship between quiet standing position and perceptibility of standing position in the
  anteroposterior direction*. J. Physiol. Anthropol. 29 (2010) 197: centro de presiones en reposo
  entre el 31 y el 58 % de la longitud del pie desde el talón.
- *Relationship between feet position and anterior-posterior center of pressure location*. J. Phys.
  Ther. Sci. 25 (2013): 44,5 ± 5,5 % de la longitud del pie con los pies separados.
- Grosor de la almohadilla plantar bajo las cabezas metatarsianas, valores de referencia ecográficos:
  Healthcare 13 (2025) 3219, <https://doi.org/10.3390/healthcare13243219>.
- Grosor de la almohadilla del talón, en pacientes con fascitis plantar y en pies sin dolor: Acta
  Orthop. Traumatol. Turc. (2019).
- Valores de referencia de baropodometría estática en adultos jóvenes: Life 15 (2025) 1354,
  <https://doi.org/10.3390/life15091354>.
- S. Prichasuk, P. Siriwongpairat, P. Mulpruek. *The heel-pad compressibility*. Clin. Orthop. Relat.
  Res. 300 (1994) 197–200: espesor sin carga 18,7 ± 2,5 mm, índice de compresibilidad 0,53 ± 0,09
  (400 personas).
- T. C. Hsu et al. *Comparison of the mechanical properties of the heel pad between young and
  elderly adults*. Arch. Phys. Med. Rehabil. (1998): índice de compresibilidad del 53 % en jóvenes y
  del 61 % en mayores.
- K. Gawronska, J. Lorkowski. *Evaluating the symmetry in plantar pressure distribution under the
  toes during standing in a postural pedobarographic examination*. Symmetry 13 (2021) 1476: presión
  bajo los dedos en bipedestación, en % de la máxima del pie.
- J. H. Hicks. *The mechanics of the foot II: the plantar aponeurosis and the arch*. J. Anat. 88
  (1954) 25–30: mecanismo de molinete de la fascia plantar.
- T. Yanagiya et al. *Athletic event-specific characteristics in floating toes during the static
  standing posture*. J. Phys. Ther. Sci. 32 (2020) 342–347: prevalencia del dedo flotante.
