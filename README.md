# brno-pisos

Bot que vigila portales de alquiler de Brno y avisa en Discord cuando un anuncio encaja con los perfiles `yo` y `grupo`. GitHub Actions programa una búsqueda cada 15 minutos. Los anuncios que requieren aclaraciones se reúnen en un resumen diario a partir de las 19:00, hora de Praga.

Lee Bezrealitky, UlovDomov, eSpolubydleni, iDNES y Bazoš. Para revisar un anuncio a mano, pega su texto o un enlace HTTPS de uno de esos portales o de Sreality en `#revisar`. [Diseño del bot](docs/superpowers/specs/2026-11-24-brno-pisos-design.md).

## Uso local

Desde la raíz del repositorio, con Python 3.12 o posterior.

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -e ".[dev]"
.venv/bin/python -m pytest -q
```

Copia `.env.example` a `.env` y rellena `WORK_LATLON` con `latitud,longitud`. Escribe los valores tal cual, sin comillas y sin `export` (el bot las leería como parte del valor o ignoraría la línea). Puedes simular la búsqueda con los portales reales y ver los avisos en la terminal sin usar Discord ni guardar el estado local.

```sh
.venv/bin/python -m pisobot --dry-run
```

La última línea contiene los contadores de la ejecución. `portales_con_error` indica cuántos recolectores han fallado. Los umbrales y perfiles se ajustan en `config.yaml`.

## Discord y GitHub Actions

1. Crea los canales `#yo`, `#grupo` y `#revisar` en un servidor privado. Crea un webhook para cada uno de los dos primeros canales. En el [portal de Discord](https://discord.com/developers/applications), crea la aplicación bot y activa **Message Content Intent**. Invítala al servidor y dale permisos para ver `#revisar`, leer el historial, enviar mensajes e insertar enlaces.
2. Añade los secretos de [`.env.example`](.env.example) en **Settings → Secrets and variables → Actions**. Usa las URLs de los dos webhooks, el token del bot y el ID de `#revisar`. Guarda las credenciales solo en `.env` local o en **Secrets** de Actions, nunca en **Variables** (`vars`): Actions las muestra sin enmascarar en los logs, que son públicos. [Cómo crear secretos del repositorio](https://docs.github.com/en/actions/how-tos/write-workflows/choose-what-workflows-do/use-secrets).
3. Ejecuta **Actions → run → Run workflow** desde la rama por defecto. El workflow publica las coincidencias actuales y guarda los cambios de estado de `data/` con el token de GitHub Actions. Si usas un fork público, habilita primero el workflow programado. [Comportamiento de los workflows en forks](https://docs.github.com/en/actions/how-tos/manage-workflow-runs/disable-and-enable-workflows).

## Servicios externos

- Rutas con Transitous para este proyecto abierto y de uso no comercial. [Política de uso](https://transitous.org/api/) y [fuentes de los datos de transporte](https://transitous.org/sources/).
- Supermercados y geocodificación con datos de [OpenStreetMap](https://www.openstreetmap.org/copyright). El bot guarda las consultas automáticas de Nominatim en caché y limita las peticiones a cuatro por minuto. Si necesitas cambiar de servidor, define `NOMINATIM_URL` en `.env` o en las variables de Actions con la URL completa de un endpoint compatible, sin claves dentro (las variables no se enmascaran). [Política de uso de Nominatim](https://operations.osmfoundation.org/policies/nominatim/).
- Resúmenes con `gemini-3.5-flash-lite` si configuras `GEMINI_API_KEY` en un proyecto con capa gratuita. El bot se guía por sus reglas cuando falta la clave o se agota la cuota. El límite de `config.yaml` es el máximo de llamadas del bot, que se aplica además de la cuota de tu proyecto. [Precios de Gemini API](https://aistudio.google.com/docs/pricing#gemini-3.5-flash-lite).
