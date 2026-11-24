# brno-pisos — diseño

Fecha: 2026-11-24. Estado: aprobado en conversación, pendiente de revisión escrita.

## Objetivo

Bot gratuito que vigila portales de alquiler de Brno, evalúa cada anuncio contra dos perfiles de búsqueda y manda a Discord un resumen de los que encajan. También evalúa a demanda los anuncios que el usuario pega en un canal (posts de Facebook, enlaces de Sreality o de amigos).

Coste: 0 €. Todo corre en GitHub Actions (repo público) con servicios gratuitos.

## Perfiles

Todos los umbrales viven en `config.yaml`, no en el código.

### `#yo`

- Tipo: habitación en piso compartido o piso entero para una persona.
- Precio real ≤ 10.000 CZK/mes. ⭐ si ≤ 8.000.
- Transporte a la oficina (coordenadas en secreto `WORK_LATLON`). Pasa si se cumple una de:
  - total ≤ 30 min en transporte público;
  - un solo tranvía directo (0 transbordos), primer tramo a pie ≤ 10 min y total ≤ 40 min.

### `#grupo`

- Tipo: piso entero con 3 o 4 dormitorios, uno por persona.
- Dormitorios: `X+1` → X; `X+kk` → X−1 (una habitación es salón con cocina).
- Precio por persona = total ÷ personas. Un piso de 4 dormitorios se evalúa para 4 y para 3 personas (una habitación queda de salón): el veredicto es el mejor de los dos y el mensaje muestra ambos precios.
  - ≤ 11.000 → ✅
  - 11.000–12.000 → ⚠️ "caro, solo si compensa"
  - > 12.000 → ❌
- `průchozí` (habitación de paso) en el texto → ⚠️.
- Transporte a `Brno hlavní nádraží`: total ≤ 30 min.

### Común

- Supermercado (`shop=supermarket` de OSM) a ≤ 640 m en línea recta (≈ 10 min andando con factor de rodeo 1,3).
- Lavadora: `pračka` → OK; `bez pračky` → ❌; sin mención → ⚠️.
- Amueblado: `nezařízený` → ⚠️; sin mención → solo informativo.
- Informativo, sin filtrar: fecha de entrada, fianza (`kauce`), comisión (`provize`), 🌳 si está a ≤ 1 km de Lužánky.

### Precio real

- Habitación: alquiler + poplatky. Si el texto dice que las energías van aparte (`+ energie`, `energie dle spotřeby`), se suma la estimación `room` (800 CZK, configurable) y el anuncio pasa a ⚠️.
- Piso entero: alquiler + poplatky + energías estimadas, marcadas como estimación en el mensaje:

  | Disposición | Energías/mes |
  |---|---|
  | `1+kk`, `1+1` | 1.000 |
  | `2+kk`, `2+1` | 1.500 |
  | `3+1`, `4+kk` | 2.500 |
  | `4+1`, `5+kk` | 3.000 |

- `elektrické topení` o `přímotop` en el texto → ⚠️ (energías muy por encima de la estimación).

### Veredictos

- ✅ pasa: todo confirmado.
- ⚠️ dudoso: nada falla explícitamente pero falta algún dato. El mensaje dice qué preguntar.
- ❌ descarta: algo falla explícitamente. No se notifica; queda en el log de la ejecución.

## Arquitectura

Un único proceso por ejecución, lanzado por `cron` de GitHub Actions cada 15 min.

```
1. collectors   leer cada portal → Listing normalizado
2. dedupe       saltar ids ya vistos y duplicados entre portales
                (huella = precio total redondeado + m² + coordenada redondeada a ~100 m)
3. prefilter    precio y disposición, antes de cualquier API externa
4. enrich       coordenadas si faltan, transporte, supermercado
5. parse_text   reglas en checo; IA solo para candidatos y solo para lo que falte
6. evaluate     aplicar los dos perfiles → ✅ / ⚠️ / ❌
7. notify       ✅ al momento; ⚠️ a la cola del resumen diario
8. review       leer #revisar, evaluar, responder
9. digest       si son ≥ 19:00 (Europe/Prague) y hoy no se ha mandado, mandar resumen de ⚠️
10. persist     commit de data/ si ha cambiado
```

### Estructura

```
brno-pisos/
  config.yaml
  pyproject.toml
  LICENSE                     MIT (requisito de Transitous)
  pisobot/
    __main__.py               orquesta los pasos 1-10
    models.py                 Listing, Evaluation, PriceBreakdown
    config.py                 carga config.yaml + secretos de entorno
    collectors/
      base.py                 interfaz Collector: fetch() -> list[Listing]
      bezrealitky.py
      ulovdomov.py
      espolubydleni.py
      idnes.py
      bazos.py
      single_url.py           una URL pegada en #revisar (incluye Sreality)
    enrich/
      geocode.py              Nominatim
      transit.py              Transitous + regla del tranvía
      supermarket.py          descarga semanal OSM + cálculo local
    parse_text.py             reglas de palabras clave en checo
    llm.py                    Gemini (capa gratuita), con fallback a reglas
    profiles.py               evaluación #yo / #grupo
    notify.py                 embeds de Discord, envío por webhook
    review.py                 lectura y respuesta en #revisar
    store.py                  data/*.json
  data/
    seen.json                 ids vistos + huellas, con fecha (se purgan > 60 días)
    cache.json                transporte por coordenada redondeada (~50 m)
    supermarkets.json         refresco semanal
    state.json                último mensaje leído de #revisar, cola ⚠️, fecha del último resumen, fallos por recolector
  tests/
    fixtures/                 respuestas reales guardadas (JSON/HTML)
  .github/workflows/run.yml
```

### Modelo `Listing`

`source, source_id, url, title, kind (room|flat), disposition, area_m2, rent, fees, fees_include_energy (bool|None), deposit, commission (bool|None), lat, lon, location_precision (exact|street|postcode), address_text, description, available_from, raw` (lo que sobre del portal).

## Fuentes

Se respeta `robots.txt` de cada portal.

| Portal | Acceso | Notas |
|---|---|---|
| Bezrealitky | `GET /vypis/nabidka-pronajem/byt/brno`, JSON en `__NEXT_DATA__` | Permitido por robots. Trae gastos, GPS, `roommate`. La API GraphQL está prohibida por robots: no se usa |
| UlovDomov | `POST ud.api.ulovdomov.cz/v1/offer/find` con bounds de Brno | Robots `Allow: /`. Habitaciones en `offerType: "coliving"` |
| eSpolubydleni | HTML `/podnajem-spolubydlici/brno` | `Crawl-delay: 10` respetado. Mezcla ofertas y búsquedas: solo "Nabízím". Sin GPS: geocodificar calle |
| iDNES | HTML `/s/pronajem/byty/.../brno/` y `/s/pronajem/byty/pokoj/brno/` | Solo rutas simples (robots bloquea combinaciones de filtros). GPS en detalle |
| Bazoš | HTML de la categoría nacional sin filtros + filtro local por PSČ de Brno | El filtro de localidad en URL está prohibido por robots. Ubicación solo por PSČ |
| Sreality | Solo vía `#revisar` (una URL pegada por el usuario) | Robots `Disallow: /`. El usuario usa la alerta oficial "Hlídací pes" |

Fuera de alcance: RealityMIX, Reality.cz (duplican agencias, poco volumen).

Peticiones por ejecución: página 1 de cada listado (ordenado por fecha) + detalle solo de anuncios nuevos que pasan el prefiltro. `User-Agent`: `brno-pisos/<versión> (+<url del repo>)`.

## Enriquecimiento

### Transporte — Transitous

- `GET https://api.transitous.org/api/v5/plan`, salida próximo día laborable 08:00 Europe/Prague (convertido a UTC), ≥ 3 itinerarios.
- Se evalúan todos los itinerarios. Tranvía directo = `transfers == 0`, un único tramo `mode == "TRAM"`, primer tramo `WALK` ≤ 600 s.
- Trolebuses llegan como `BUS` (`routeType 800`): no cuentan como tranvía (configurable).
- Caché por coordenada redondeada a ~50 m y destino.
- Política de uso: proyecto open source no comercial, `User-Agent` con app + contacto (URL del repo, nunca email personal), atribución "Rutas: Transitous" en cada mensaje. Umbral de uso alto: 720 `/plan` al día; el volumen esperado es muy inferior.
- Fallo: el anuncio queda ⚠️ "transporte sin calcular" y se reintenta en la siguiente ejecución.

### Supermercado

- Overpass API, una vez por semana: todos los `shop=supermarket` del área de Brno → `data/supermarkets.json`.
- Por anuncio: distancia en línea recta al más cercano, cálculo local.

### Geocodificación

- Nominatim (1 petición/s, `User-Agent` propio) solo para fuentes sin GPS.
- `location_precision` distinto de `exact` → el mensaje avisa de que tiempos y distancias son aproximados. Si no hay calle ni PSČ → ⚠️.

## Lectura de texto

1. `parse_text.py`: reglas de palabras clave en checo para `pračka`, `včetně energií`, `poplatky`, `kauce`, `provize`, `průchozí`, `nezařízený`, `elektrické topení`, fechas.
2. `llm.py` (Gemini, capa gratuita): solo para candidatos que ya pasaron prefiltro y transporte. Devuelve JSON con resumen en español (2-3 frases) y los campos que las reglas dejaron en "desconocido". Las coincidencias explícitas de las reglas mandan sobre la IA.
3. En `#revisar` con texto libre, la IA extrae además precio, gastos y calle.
4. Si la IA falla o se agota la cuota: se sigue solo con reglas y el resumen es un fragmento del texto en checo.

Límites exactos de la capa gratuita: verificar al implementar. Tope de llamadas por día en `config.yaml`.

## Discord

- Servidor privado con `#yo`, `#grupo`, `#revisar`.
- Webhook para `#yo` y otro para `#grupo` (envío). Bot con token y `Message Content` intent para leer y responder en `#revisar` vía REST (sin conexión persistente).
- Formato de embed:

```
✅ ⭐ Pokoj 16 m², Veveří: 8.300 CZK/mes en total
🏠 Habitación · Bezrealitky · sin comisión
💰 6.500 alquiler + 1.800 poplatky (energías incluidas)
🚋 oficina: 22 min · tranvía 4 directo · 5 min andando
🛒 Albert · 300 m · ~4 min
🧺 Lavadora: sí · 🛋️ Amueblado: sí · 🌳 Lužánky 600 m
📅 Libre desde 1.2.2027 · Fianza 16.600
❓ Preguntar: (solo en ⚠️)
📝 Resumen en español (o fragmento en checo sin IA)
🔗 enlace
Rutas: Transitous
```

- `#grupo`: precio por persona y transporte a `hlavní nádraží`.
- Frecuencia: ✅ al momento; ⚠️ en un resumen diario a partir de las 19:00 Europe/Prague (primera ejecución tras esa hora; tolera retrasos del cron).
- `#revisar`: el bot lee mensajes posteriores al último procesado, evalúa contra los dos perfiles y responde citando el mensaje. Si falta la calle, la pide. También recibe el aviso cuando un recolector falla 3 ejecuciones seguidas.
- Primera ejecución: notifica los anuncios actuales que cumplen, no solo los nuevos.

## Secretos (GitHub Secrets)

`DISCORD_WEBHOOK_YO`, `DISCORD_WEBHOOK_GRUPO`, `DISCORD_BOT_TOKEN`, `DISCORD_REVISAR_CHANNEL_ID`, `GEMINI_API_KEY`, `WORK_LATLON`.

## GitHub Actions

- Repo público (minutos ilimitados en runners estándar). Python 3.12, dependencias: `httpx` (también para Gemini vía REST, sin SDK), `selectolax`, `pyyaml`; tests con `pytest`.
- `run.yml`: `schedule: */15 * * * *` + `workflow_dispatch`. `concurrency` para que nunca se solapen dos ejecuciones. Permiso `contents: write` para el commit de `data/`.
- Commit de `data/` solo si cambia, con autor genérico del bot. Esos commits mantienen el repo activo (Actions desactiva `cron` tras 60 días sin actividad).
- `test.yml`: `pytest` en cada push.
- El `cron` de GitHub puede retrasarse 5-30 min en horas de carga: asumido.

## Errores

- Cada recolector se ejecuta aislado (try/except). Un fallo no para a los demás. Contador de fallos consecutivos en `state.json`; aviso a `#revisar` al llegar a 3 y otro al recuperarse.
- Fallos de Transitous, Nominatim o IA degradan el anuncio a ⚠️ o al modo sin IA; nunca lo descartan.
- Fallo al enviar a Discord: el anuncio no se marca como visto y se reintenta.

## Tests

- Sin red. Cada recolector se prueba contra una respuesta real guardada en `tests/fixtures/`.
- Unitarios para `parse_text` (frases reales en checo), `profiles` (casos frontera de precio, dormitorios, tranvía directo), `transit` (respuesta Transitous guardada), `supermarket` (distancias), formato de `notify`.
- Al implementar cada recolector se verifica una vez contra el portal real y se guarda la fixture.

## Fuera de alcance (v1)

Rastreo automático de Sreality, lectura automática de Facebook, alertas de bajada de precio, imágenes, servidor MCP, varias horas de salida, RealityMIX y Reality.cz.

## Puesta en marcha (pasos del usuario)

1. Crear servidor de Discord con los tres canales, dos webhooks y la aplicación bot (con `Message Content`), e invitar el bot a `#revisar`.
2. Crear clave de Gemini API (capa gratuita).
3. Crear el repo público en GitHub y cargar los secretos.
4. Activar la alerta "Hlídací pes" de Sreality en su app.
