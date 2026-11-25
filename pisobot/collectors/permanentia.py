"""permanentia.cz: agencia RE/MAX de Brno. HTML del servidor, un puñado de pisos y a veces ninguno."""
from __future__ import annotations

import dataclasses
import json
import re
from datetime import date
from urllib.parse import urljoin

import httpx

from pisobot.collectors.base import HTMLParser, block_text, inline_text, price_czk
from pisobot.models import Kind, Listing
from pisobot.parse_text import norm, normalize_disposition, parse_area

BASE = "https://www.permanentia.cz"
# Solo el filtro de ciudad (Brno): su robots.txt veta ordenar y filtrar por precio o superficie
LIST_URL = f"{BASE}/pronajem/byty/jihomoravsky/?idMesta%5B0%5D=582786"
_ID = re.compile(r"/reality/[^/?#]*?-(\d+)/?(?:[?#]|$)")
# El enlace "Navigovat k místu" lleva las coordenadas del inmueble: /maps/dir/Current+Location/49.20,16.60
_GMAPS = re.compile(r"/maps/dir/[^/]*/(-?\d+(?:\.\d+)?),(-?\d+(?:\.\d+)?)")
# "pokoj" suelto es una habitación; "2 pokoje", "dvoupokojový" o "2-pokojový" describen un piso entero
_ROOM = re.compile(r"(?<![\w-])(?<!\d )pokoj|spolubydl")
# "9 500 Kč / měsíc": con otra unidad delante ("Kč / m² / měsíc"), detrás ("Kč / měsíc / m²", "/ osobu", "/ den") o sin
# ella no se sabe cuánto es al mes. El \b obliga a leer "měsíc" entero: sin él el retroceso lo recortaría y esquivaría
# el rechazo
_MONTHLY = re.compile(r"Kč\s*/\s*měs\w*\b(?!\.?\s*/|\.?\s+(?:na|za)\s+(?:osob|m2|m²))", re.IGNORECASE)
# "Bayerova, Brno", "Střední, Brno - Ponava", "Brno-Líšeň", "Brno": calle opcional, ciudad y barrio opcional
_PLACE = re.compile(r"(?:(?P<street>.+?),\s*)?Brno(?:\s*[-–]\s*(?P<quarter>.+))?", re.IGNORECASE)
# "Brno - město" o "Brno-město" no es un barrio: no sitúa el anuncio
_CITY = re.compile(r"(?:brno\s*[-–]\s*)?město", re.IGNORECASE)
_NO_LISTINGS = "Permanentia: el listado no trae anuncios (¿cambió el formato?)"
_NO_DETAIL = "Permanentia: el detalle no trae la ficha del anuncio (¿cambió el formato?)"
_NO_DATA = "Permanentia: el detalle no trae datos del anuncio (¿cambió el formato?)"
# Los importes se buscan en el texto ya normalizado (sin tildes ni mayúsculas). Con decimales ("2 500,50 Kč") no hay
# importe fiable: mejor None que uno falso
_AMOUNT = r"(?P<amount>\d{1,3}(?:[ .]\d{3})+|\d+)(?![.,]\d)\s*(?:,-\s*)?(?:kc|czk|,-)"
# Entre el concepto y el importe solo caben conectores: "činí", "ve výši", dos puntos
_GLUE = r"(?:\s*:|\s+(?:cini|ve vysi|jsou|je|cca))*\s*"
# "záloha" sin más suele ser la fianza: solo cuenta como gastos si es un anticipo de servicios
_FEES = re.compile(rf"\b(?:na|za)\s+sluzby{_GLUE}{_AMOUNT}")
# "Provize za služby činí 14 520 Kč" también casa con _FEES, pero es la comisión: se mira la frase que lo precede
_BROKER_FEE = re.compile(r"proviz|zprostredkovatel")
_CLAUSE_END = re.compile(r"[.,;]\s")
_DEPOSIT = re.compile(rf"\bkauc\w*{_GLUE}{_AMOUNT}")
# Sin comas, puntos ni punto y coma entre el concepto y el importe: un importe de otra frase no es la comisión
_COMMISSION = re.compile(
    rf"\b(?:zprostredkovatelsk\w*\s+poplat\w*|proviz\w*)(?P<gap>[^.,;]*?){_AMOUNT}"
)
# Lo que hay entre "provize" y el importe niega la comisión: "está incluida en el alquiler 9 500 Kč", "no se cobra,
# alquiler 9 500 Kč". "bez DPH" solo aclara el importe
_NO_COMMISSION = re.compile(r"zahrnut|\bneni\b|\bnejsou\b|neuctuj|nehrad|\bzadn|\bbez\b(?!\s+dph)")
# Encabezado del bloque de atributos que se añade a la descripción: ninguna palabra suya dispara una regla de parse_text
_PARAMS_LABEL = "Parametry inzerátu:"
# Ya se leen aparte o no describen el piso (las etiquetas de superficie llevan la unidad: "Užitná plocha m2")
_SKIPPED_PARAMS = (
    "Kategorie", "Dispozice bytu", "Užitná plocha", "Celková podlahová plocha", "K dispozici od", "Datum nastěhování",
)
# "Vybaveno: Ne" contiene "vybaven" y parse_text lo leería como amueblado: se reescribe con palabras que sus reglas
# entienden. Un valor que no se conoce no se escribe
_FURNISHED = {"ano": "Vybaveno", "castecne": "Částečně vybaveno", "ne": "Nevybaveno"}
# Cualquier otro atributo con estas raíces ("Vybavení: Kuchyňská linka", "Vybavenost: Ano") daría amueblado sin serlo
_FURNISHED_WORDS = re.compile(r"vybaven|zarizen|furnished")
# Los bloques de la ficha: sin ninguno, la página tiene un título pero no es un anuncio legible
_DETAIL_BLOCKS = (
    ".detailPopis, .estateParams, .estate-parameters, .estateDetail-adresa, .estateDetail-cena, .estateDetail-map"
)


def _kind(title: str) -> Kind:
    return Kind.ROOM if _ROOM.search(norm(title)) else Kind.FLAT


def _rent(raw: str) -> int | None:
    return price_czk(raw) if _MONTHLY.search(raw) else None


def _area(kind: Kind, title: str) -> int | None:
    # En una habitación no se sabe si los m² son los del cuarto o los del piso: mejor sin dato que uno que despiste
    return None if kind is Kind.ROOM else (parse_area(title) or None)


def _square_meters(value: str | None) -> int | None:
    """La superficie de la ficha va sin unidad ("18") o con ella ("18,5 m²")."""
    match = re.fullmatch(r"(\d+(?:[.,]\d+)?)(?:\s*m2)?", norm(value or ""))
    return (int(float(match[1].replace(",", "."))) or None) if match else None


def _place(info: str) -> tuple[str, str]:
    """(address_text, location_precision). Con calle se geocodifica la calle; solo con barrio, su centroide queda a
    kilómetros de la casa y se marca aproximado; sin ninguno de los dos no hay ubicación (no se geocodifica Brno)."""
    if not info:
        return "", "none"
    match = _PLACE.fullmatch(info)
    if match is None:
        return info, "postcode"
    if match["street"]:
        return info, "street"
    quarter = (match["quarter"] or "").strip()
    if not quarter or _CITY.fullmatch(quarter):
        return "", "none"
    return info, "postcode"


def _date(raw: str | None) -> date | None:
    match = re.search(r"(\d{1,2})\.\s*(\d{1,2})\.\s*(\d{4})", raw or "")
    if not match:
        return None
    day, month, year = (int(part) for part in match.groups())
    try:
        return date(year, month, day)
    except ValueError:
        return None


def _listing(*, source_id: str, url: str, title: str, price: str, info: str, needs_detail: bool) -> Listing:
    kind = _kind(title)
    address, precision = _place(info)
    return Listing(
        source="permanentia",
        source_id=source_id,
        url=url,
        title=title,
        kind=kind,
        rent=_rent(price),
        disposition=normalize_disposition(title),
        area_m2=_area(kind, title),
        address_text=address,
        location_precision=precision,
        needs_detail=needs_detail,
    )


def _title(card, link) -> str:
    """El listado recorta los títulos largos con "..."; el pie de la foto trae el completo."""
    title = inline_text(link)
    if title.endswith(("...", "…")):
        image = card.css_first("img[alt]")
        caption = " ".join((image.attributes.get("alt") or "").split()) if image is not None else ""
        full = re.sub(r"^Fotografie nemovitosti\s*-\s*", "", caption)
        if full.startswith(title.rstrip(".… ")):
            return full
    return title


def _no_results(tree) -> bool:
    """Página de resultados que existe pero no tiene ningún anuncio: ni tarjetas ni enlaces a fichas."""
    container = tree.css_first("div.estateList")
    return (
        container is not None
        and container.css_first("article.estateListItem") is None
        and not any(_ID.search(link.attributes.get("href") or "") for link in container.css('a[href^="/reality/"]'))
    )


def _total_count(tree) -> int | None:
    """Cuántos anuncios devuelve el filtro según el JSON de la página, con independencia del marcado de las tarjetas.
    None si el JSON falta o no trae un entero."""
    script = tree.css_first("script#data-default-preview-data")
    if script is None:
        return None
    try:
        total = json.loads(script.text())["totalCount"]
    except (ValueError, KeyError, TypeError):
        return None
    return total if isinstance(total, int) and not isinstance(total, bool) and total >= 0 else None


def parse_list(html: str, *, allow_empty: bool = True) -> list[Listing]:
    """allow_empty: la agencia puede quedarse sin pisos en Brno sin que sea un fallo, siempre que la página siga
    teniendo su estructura de resultados y su recuento no diga lo contrario."""
    tree = HTMLParser(html)
    listings = []
    for card in tree.css("article.estateListItem.js-estateListItem"):
        link = card.css_first('h2 a[href^="/reality/"]')
        href = link.attributes.get("href") if link is not None else None
        match = _ID.search(href or "")
        if not match:
            continue
        listings.append(_listing(
            source_id=match.group(1),
            url=urljoin(BASE, href),
            title=_title(card, link),
            price=inline_text(card.css_first("span.cena")),
            info=inline_text(card.css_first("div.parametry")),
            needs_detail=True,
        ))
    # Una página sin su estructura falla siempre; una vacía solo si no se ha pedido tolerarlo. Si el recuento de la
    # página dice que hay anuncios, no son cero aunque el marcado de las tarjetas haya cambiado
    if not listings and (_total_count(tree) or not (allow_empty and _no_results(tree))):
        raise ValueError(_NO_LISTINGS)
    return listings


def _params(tree) -> dict[str, str]:
    """Pares etiqueta/valor de "Podrobné informace". Una fila sin valor es un campo vacío del CRM."""
    params = {}
    for row in tree.css(".estateParams .parameters div.row"):
        cells = [node for node in row.iter() if node.tag == "span"]
        if len(cells) >= 2:
            key, value = inline_text(cells[0]), inline_text(cells[1])
            if key and value:
                params[key] = value
    return params


def _param(params: dict[str, str], label: str) -> str | None:
    return next((value for key, value in params.items() if key.startswith(label)), None)


def _note(tree) -> str:
    """La "Poznámka" del precio: ahí van los gastos y la comisión cuando el texto libre no los dice."""
    for parameter in tree.css(".estate-parameters .parameter"):
        key = parameter.css_first(".parameter-key")
        if key is not None and norm(inline_text(key)).rstrip(":") == "poznamka":
            return inline_text(parameter.css_first(".parameter-value"))
    return ""


def _extras(params: dict[str, str]) -> str:
    """Los atributos de la ficha ("Vybaveno: Ne", "Topení", "Ostatní: Pračka"...) solo salen ahí: sin añadirlos al
    texto las reglas de palabras clave no los ven. El encabezado no puede disparar ninguna regla."""
    items = []
    for key, value in params.items():
        if key.startswith(_SKIPPED_PARAMS):
            continue
        name = norm(key)
        if name != "vybaveno" and _FURNISHED_WORDS.search(f"{name} {norm(value)}"):
            continue
        if name == "vybaveno":
            item = _FURNISHED.get(norm(value))
        elif name == "topeni":
            # "Lokální elektrické" solo casa con la regla de calefacción eléctrica pegado a "topení"
            item = f"{value} topení"
        else:
            item = f"{key}: {value}"
        if item:
            items.append(item)
    return f"{_PARAMS_LABEL} {' | '.join(items)}" if items else ""


def _amount(match: re.Match | None) -> int | None:
    return (int(re.sub(r"\D", "", match["amount"])) or None) if match else None


def _fees(text: str) -> int | None:
    for match in _FEES.finditer(text):
        clause = _CLAUSE_END.split(text[:match.start()])[-1]
        if not _BROKER_FEE.search(clause):
            return _amount(match)
    return None


def _commission(text: str) -> bool:
    for match in _COMMISSION.finditer(text):
        # La negación puede ir justo antes del concepto ("bez provize - nájem 9 500 Kč") o entre concepto e importe
        before = _CLAUSE_END.split(text[:match.start()])[-1][-20:]
        if _amount(match) is not None and not _NO_COMMISSION.search(before + match["gap"]):
            return True
    return False


def _gps(tree) -> tuple[float | None, float | None]:
    link = tree.css_first(".estateDetail-map a.navigovat")
    match = _GMAPS.search(link.attributes.get("href") or "") if link is not None else None
    if not match:
        return None, None
    lat, lon = float(match.group(1)), float(match.group(2))
    return (lat, lon) if abs(lat) <= 90 and abs(lon) <= 180 else (None, None)


def parse_detail(html: str, listing: Listing | None = None, url: str = "") -> Listing:
    tree = HTMLParser(html)
    heading = tree.css_first(".estateDetail-title h1")
    # Un anuncio retirado puede redirigir al listado: sin su ficha no es un detalle
    if heading is None:
        raise ValueError(_NO_DETAIL)
    # Un título suelto no es un anuncio: sin ningún bloque de datos el formato habría cambiado
    if tree.css_first(_DETAIL_BLOCKS) is None:
        raise ValueError(_NO_DATA)
    if listing is None:
        match = _ID.search(url)
        if not match:
            raise ValueError(f"Permanentia: la URL no lleva el id del anuncio ({url})")
        # El precio y la dirección se leen abajo, igual que con un anuncio que viene del listado
        listing = _listing(
            source_id=match.group(1), url=url, title=inline_text(heading), price="", info="", needs_detail=False,
        )
    address_node = tree.css_first(".estateDetail-adresa")
    if address_node is not None:
        for link in address_node.css("a"):  # el enlace "mapa" va dentro de la dirección
            link.decompose()
    address, precision = _place(inline_text(address_node))
    # Si el detalle no sitúa mejor el piso, se queda lo que ya traía el listado
    if precision == "none":
        address, precision = listing.address_text, listing.location_precision
    params = _params(tree)
    note = _note(tree)
    parts = (block_text(tree.css_first(".detailPopis")), f"Poznámka: {note}" if note else "")
    prose = "\n".join(part for part in parts if part)
    # Importes y comisión solo del texto libre y la nota: la lista de parámetros que se añade no los trae
    text = norm(prose)
    fees, deposit = _fees(text), _amount(_DEPOSIT.search(text))
    # "Zprostředkovatelský poplatek" no lo ve parse_text
    commission = _commission(text)
    lat, lon = _gps(tree)
    # La superficie útil manda sobre la total; en una habitación ninguna es la del cuarto
    area = None if listing.kind is Kind.ROOM else (
        _square_meters(_param(params, "Užitná plocha")) or _square_meters(_param(params, "Celková podlahová plocha"))
    )
    # Lo que el detalle no trae no borra lo que ya había
    return dataclasses.replace(
        listing,
        description="\n".join(part for part in (prose, _extras(params)) if part) or listing.description,
        rent=listing.rent or _rent(inline_text(tree.css_first(".estateDetail-cena"))),
        fees=fees if fees is not None else listing.fees,
        deposit=deposit if deposit is not None else listing.deposit,
        commission=True if commission else listing.commission,
        disposition=normalize_disposition(_param(params, "Dispozice bytu")) or listing.disposition,
        area_m2=area or listing.area_m2,
        address_text=address,
        location_precision="exact" if lat is not None else precision,
        lat=lat if lat is not None else listing.lat,
        lon=lon if lon is not None else listing.lon,
        available_from=_date(_param(params, "K dispozici od")) or _date(_param(params, "Datum nastěhování"))
        or listing.available_from,
        needs_detail=False,
    )


class Permanentia:
    name = "permanentia"

    def __init__(self, http: httpx.Client):
        self.http = http

    def fetch_list(self) -> list[Listing]:
        response = self.http.get(LIST_URL)
        response.raise_for_status()
        return parse_list(response.text)

    def fetch_detail(self, listing: Listing) -> Listing:
        response = self.http.get(listing.url)
        response.raise_for_status()
        return parse_detail(response.text, listing)
