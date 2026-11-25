# Corte de plantilla y revisión por IA

## 1. Corte de plantilla (sin IA)

Hasta ahora la planta se extraía **por pendiente**: la región crecía desde la planta hasta que la
piel se inclinaba más del *Ángulo máximo* (62°). Eso deja fuera justo lo que necesita una plantilla
personalizada: la copa del talón, la pestaña bajo el arco y algo de pared lateral. En ortopedia el
límite de una plantilla es la **línea de recorte** (*trim line*) y se define por **altura sobre el
suelo**: la copa del talón suele estar entre 10 y 20 mm (≤ 10 mm se considera baja), y la pestaña
medial llega a la altura del arco.

El modo `plantilla` (ahora el predeterminado; `planta` conserva el comportamiento anterior) hace:

1. **Planta** con la segmentación de siempre (pendiente, pliegues, capas, islas de dedos…).
2. **Suelo**: plano de la envolvente convexa inferior de la planta bajo la línea de carga
   (trípode talón – cabezas metatarsianas). Altura de cada punto = distancia a ese plano.
3. **Marco anatómico**: eje talón → dedos (el antepié es más ancho que el talón), lado medial (en
   el mediopié el arco interno queda más alto que el borde lateral) y altura del arco medida.
4. **Línea de recorte objetivo** por zonas, con transiciones suaves:

   | Zona | Parámetro | Por defecto |
   |---|---|---:|
   | Talón (copa) | `heel_cup` | 15 mm |
   | Mediopié medial (pestaña del arco) | `medial_flange` | auto = altura del arco + 6 mm |
   | Mediopié lateral | `lateral_flange` | 10 mm |
   | Antepié | `forefoot_wall` | 6 mm |
   | Dedos | `toe_wall` | 4 mm |
   | En todo el contorno, por encima del borde de la planta | `wall_min` | 2 mm |

5. **Paredes**: la selección crece desde la planta por las caras conectadas que miran al suelo
   (pendiente ≤ `wall_max_angle`, 80°), son visibles desde abajo (sin voladizos ni capas ocultas) y
   quedan por debajo de la línea de recorte. El empeine, el tobillo, un ala o un suelo unidos por
   una pared más alta quedan fuera.
6. **Cobertura**: se compara el resultado con lo que el escaneo permite conservar (caras visibles
   desde el suelo, conectadas a la planta y por debajo de la línea de recorte): fracción cubierta en
   talón, mediopié, antepié y dedos, milímetros que faltan por la punta y por el talón, altura de
   pared alcanzada y geometría que sobra. Si falta algo, **se reajustan los parámetros y se vuelve a
   cortar** (hasta 3 intentos; se queda el mejor). Ejemplo: si faltan dedos se amplían la distancia
   y el salto de altura de las islas y el ángulo máximo; si falta pared, la pendiente admitida.

La planta se remalla como campo de alturas (2,5D), que la representa muy bien; la **pared se
construye aparte**, como una franja de columnas a lo largo del borde de la planta: cada columna
sube siguiendo la pared del escaneo (cortes horizontales cada 0,5 mm) hasta la línea de recorte, y
los desplazamientos se suavizan a lo largo del borde. Así la pared no tiene escalones y el borde de
la plantilla queda limpio (un campo de alturas en rejilla daba escalones y dientes de sierra en las
paredes casi verticales). La pared nunca forma voladizos vista desde el suelo. Para la simulación de presión las paredes no cuentan
(solo las caras a menos de 65° del eje plantar), porque no tocan el suelo.

## 2. Revisión por IA (Claude)

La IA hace lo que haría un técnico mirando la pantalla: ve el corte (o el mapa de presión), dice si
está bien y, si no, **qué parámetros cambiar**; el programa vuelve a calcular y le enseña el nuevo
resultado. La geometría y la física las calcula siempre el código; la IA solo elige parámetros
**dentro de límites fijos** (los valores fuera de rango se recortan y los parámetros desconocidos se
ignoran). Si un cambio empeora claramente la cobertura medida, se deshace.

### Qué ve la IA

- **Corte**: una imagen con cuatro vistas (escaneo desde el suelo con lo conservado en color;
  superficie resultante coloreada por altura; perfil medial/lateral con la línea de recorte
  objetivo; cortes transversales en talón, arco, antepié y dedos), la cobertura medida y la tabla de
  parámetros con sus límites. La misma imagen se puede ver en la interfaz («Ver las vistas que
  analiza la IA») o guardar con `cli.py --vistas vistas.png`.
- **Presión**: el mapa bilateral, el reparto por regiones y por dedo, los avisos del modelo y los
  rangos de referencia de la baropodometría estática (retropié ~55–66 %, dedos separados, hallux
  ≈ 20–26 % de la presión máxima, 2.º dedo ≈ 18–24 %, 3.º–5.º ≈ 13–16 %).

### Cómo se usa

La revisión por IA **forma parte del proceso por defecto**: no hay que pulsar nada aparte.

- **Al abrir el programa** (cualquiera de las dos páginas) se comprueba la conexión con Claude. Si no
  hay clave, o no funciona, aparece una ventana que la pide: pegue la clave, deje el modelo
  *Claude Sonnet 5.5* (por defecto) y pulse *Conectar*. Se hace una llamada mínima a la API para
  comprobar clave, créditos y modelo, y la clave queda guardada en `.env` de este ordenador: las
  siguientes veces se conecta sola. Arriba se ve el estado (*IA conectada · Claude Sonnet 5.5*);
  pulsándolo, o con *Cambiar clave o modelo*, se cambia la clave o el modelo.
- **Interfaz de un pie**: pulse *▶ Procesamiento automático completo*. El programa corta, la IA revisa y reajusta hasta darlo por
  bueno (*Rondas máx.*, 6 por defecto) y después se suaviza, se crea el sólido y se valida.
- **Interfaz bilateral**: *Procesar ambos escaneos* hace lo mismo con cada pie; *Simular* revisa y
  reajusta además el mapa de presión. Los botones *Repetir la revisión…* vuelven a pasarla.
- Sin clave, el proceso se detiene y lo indica. La casilla *Sin IA (solo pruebas)* procesa sin ella.
- **Línea de comandos** (con IA por defecto; `--sin-ia` la desactiva, `--ia N`, `--ia-corte N` y
  `--ia-presion N` fijan las rondas máximas):

  ```bash
  export ANTHROPIC_API_KEY=sk-ant-...            # o guárdela en el archivo .env (ver abajo)
  .venv/bin/python cli.py escaneo.stl -o plantilla.stl --vistas vistas.png
  .venv/bin/python cli_bilateral.py izq.stl der.stl --peso 70 -o salida/
  ```

### La clave

Se busca en este orden: variable de entorno `ANTHROPIC_API_KEY`; la introducida en la interfaz (en
memoria); el archivo `.env` de la carpeta del proyecto con la línea `ANTHROPIC_API_KEY=sk-ant-...`.
`.env` está en `.gitignore`: **no se sube a GitHub**. No pegue la clave en el código, en un *issue*
ni en un chat. En GitHub Codespaces, guárdela como *secret* de Codespaces con el nombre
`ANTHROPIC_API_KEY` (Settings → Codespaces → Secrets) y estará disponible como variable de entorno.

El modelo por defecto es `claude-sonnet-5-5`; se cambia en la interfaz, con `--modelo` o con
`ANTHROPIC_MODEL` en `.env`.

### Créditos y coste

La API se paga aparte de la suscripción de claude.ai, con **créditos de prepago** en la
**Claude Console**: cree una cuenta en `platform.claude.com`, vaya a *Settings → Billing*, pulse
*Buy credits* y, en *API keys*, cree la clave (si pregunta dónde se ejecuta el código, elija
«en otro sitio» / *Somewhere else*; ámbito *Default workspace*). Con un plan Max, Ultra o Team se pueden reclamar
además los créditos mensuales de API incluidos vinculando una organización de la Console.

Cada ronda envía una imagen y unas cifras (del orden de 3 000–5 000 tokens de entrada y unos
cientos a pocos miles de salida). Con Claude Sonnet 5.5 (2 $ por millón de tokens de entrada, 10 $
por millón de salida) procesar un par de pies con su simulación (hasta 6 rondas en cada paso) cuesta
del orden de céntimos a pocas décimas de dólar. La interfaz y la CLI
muestran las llamadas y los tokens usados.

### Privacidad

Solo se envían a la API las imágenes de diagnóstico y las cifras (no el STL). Las imágenes muestran
la forma del pie.
