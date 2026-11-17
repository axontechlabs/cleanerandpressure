# Modo bilateral: simulación de bipedestación estática con los dos pies

Página `http://127.0.0.1:8000/bilateral` (botón **Modo bilateral (dos pies) →** en la aplicación de un
pie). La aplicación de un pie no cambia; este modo es una función añadida con su propia sesión.

## Flujo

1. **Escaneos.** Un STL para el pie izquierdo y otro para el derecho, en campos separados.
2. **Limpieza.** *Procesar ambos escaneos* ejecuta, por separado, el mismo procesamiento automático
   de la aplicación de un pie (segmentación, contorno, suavizado, sólido y validación). Resultado:
   dos STL limpios e independientes, que se descargan en el marco de su archivo original. Si alguno
   no supera la validación, no se exporta y no se puede simular.
3. **Persona.** Peso corporal (kg) y reparto izquierdo/derecho (50/50 por defecto), o bien la
   proyección del centro de masas si se conoce.
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
ejes largos de los pies** (7° de apertura por pie). El propio estudio muestra una gran variabilidad
entre personas, así que separación, apertura de cada pie y avance de uno respecto al otro son
ajustables. La posición natural de una persona concreta no se puede deducir de dos escaneos
independientes: no contienen la relación entre los pies. Si con los valores indicados las huellas se
solapan o quedan a menos de 8 mm, la separación se aumenta automáticamente y se avisa.

Las transformaciones de colocación se aplican solo a la copia usada en la simulación; los STL
limpios no cambian (lo comprueba una prueba automática).

## Modelo biomecánico

El pie se modela con varios segmentos y se carga en este orden, como ocurre al ponerse de pie:

1. **Asentamiento por gravedad.** El pie, como sólido rígido cargado en su línea de carga, cae
   sobre el suelo y queda apoyado en el plano de su envolvente convexa que contiene esa línea
   (normalmente talón y cabezas metatarsianas). El informe indica qué regiones tocan el suelo en
   este momento, antes de comprimir el tejido.
2. **Dedos.** Cada dedo tiene su propia articulación metatarsofalángica (MTF). Por franjas
   mediolaterales de ~9 mm, cada dedo gira hacia el suelo hasta apoyar, dentro del rango de flexión
   plantar (35°). En un escaneo sin carga los dedos suelen quedar algo levantados; así apoyan como en
   bipedestación real, con contacto ligero. Bajo carga, el segmento de los dedos está en equilibrio de
   momentos: la reacción del suelo bajo los dedos equilibra el tono de los flexores (400 N·mm por pie
   con medio peso) más la rigidez pasiva de la articulación (4000 N·mm/rad).
3. **Arco interno.** El arco longitudinal interno desciende con la carga. La caída del navicular
   en adultos sanos es de unos 3,6–8,1 mm de media; por defecto se usan 6 mm con medio peso por pie,
   proporcional a la carga. El descenso es máximo bajo el navicular y nulo en la columna lateral
   (mucho más rígida). Solo cierra la holgura hasta el suelo: el esqueleto no empuja el tejido por sí
   mismo. Si el escaneo se tomó de pie, marque *Escaneo tomado en carga* y no se aplica.
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
de carga cae al 32 % de la longitud del pie desde el talón, en la línea media de la huella, porque
en bipedestación tranquila el centro de presiones queda justo por delante de los tobillos. Con el
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
```

## Pruebas (`tests/test_bilateral.py`)

Pareja sintética con arcos distintos (16 y 22 mm) y el derecho obtenido por simetría, cada uno con
su propia orientación aleatoria. Se comprueba:

- dos sólidos válidos e independientes;
- la detección del lado y el aviso si los archivos están cruzados;
- el equilibrio exacto de fuerzas con repartos 50/50, 45/55 y 62/38;
- el equilibrio de momentos de cada pie (centro de presiones a menos de 0,5 mm de la línea de carga);
- que los dedos apoyan (más de 2 cm²) con carga ligera (menos del 10 %) y que sin tono flexor
  cargan menos;
- el reparto de referencia: talón 50–72 %, mediopié < 15 %, antepié 20–40 %, pico en el talón;
- el descenso del arco y su desactivación para escaneos en carga;
- rangos fisiológicos (pico entre 50 y 250 kPa, contacto entre 60 y 180 cm² por pie, retropié por
  encima del 30 %);
- que pies distintos dan mapas distintos y que las regiones suman la fuerza del pie;
- el modo de centro de masas, incluido el caso sin equilibrio posible;
- que no hay solapes y que los STL limpios no cambian al colocar los pies;
- las exportaciones, que las propiedades de un pie no afectan al otro y la API web.

Con su escaneo real (y su copia especular como segundo pie), 70 kg y 50/50:

| Indicador | Valor por pie |
|---|---:|
| Contacto | 115 cm², de ellos 13–16 cm² bajo los dedos |
| Talón / mediopié / antepié / dedos | 50 / 28 / 19 / 3 % |
| Pico en el talón / antepié / dedos | 183 / 85 / 25 kPa |

Los cinco dedos apoyan con contacto ligero. El mediopié carga más que la media de Cavanagh porque
el borde lateral de ese escaneo ya está prácticamente apoyado sin carga.

Con la pareja sintética, el reparto es talón 62 %, mediopié 4–5 % y antepié 34 %, con contacto
ligero de los dedos.

Son comprobaciones de verosimilitud frente a la literatura, no una validación.

## Limitaciones

- **Sin validar frente a mediciones.** Ninguna presión simulada se ha comparado con una plataforma.
  Los módulos del tejido son un valor de partida; para usar las cifras absolutas haría falta
  calibrarlos con al menos una medida baropodométrica de la misma persona.
- **Arco simplificado.** El descenso del arco es un valor fijo (6 mm con medio peso) con una forma
  típica, no se calcula a partir de la rigidez de cada pie. Un pie muy flexible o muy rígido necesita
  ajustarlo; se puede medir con la prueba de caída del navicular. Si el escaneo se hizo en carga,
  hay que desactivarlo.
- **Dedos.** Se asientan por franjas de ~9 mm, no dedo a dedo identificado. El tono flexor es un
  valor típico; en pies con dedos en garra o hallux limitus el resultado de los dedos es poco fiable.
- **Postura.** La separación y la apertura por defecto son la media de una población, no las de la
  persona.
- **Línea de carga.** Su posición (38 %) es una hipótesis razonable para bipedestación tranquila, no
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
  (1999) 623–628.
- J. C. Teoh, B. Lim, T. Lee. *Effect of deformation depth on plantar soft tissue behavior*. J. Foot
  Ankle Res. 7 (Suppl. 1) A84 (2014): umbral no lineal del 12 % (talón) y el 16 % (2.ª cabeza
  metatarsiana).
- R. G. Nielsen et al. *Determination of normal values for navicular drop during walking*. J. Foot
  Ankle Res. (2009): caída del navicular de 3,6–8,1 mm de media en adultos sanos.
- Grosor de la almohadilla plantar bajo las cabezas metatarsianas, valores de referencia ecográficos:
  Healthcare 13 (2025) 3219, <https://doi.org/10.3390/healthcare13243219>.
- Grosor de la almohadilla del talón, en pacientes con fascitis plantar y en pies sin dolor: Acta
  Orthop. Traumatol. Turc. (2019).
- Valores de referencia de baropodometría estática en adultos jóvenes: Life 15 (2025) 1354,
  <https://doi.org/10.3390/life15091354>.
