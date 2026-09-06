import os
import re
import sys
from datetime import datetime, timezone
from email.utils import format_datetime, parsedate_to_datetime
from pathlib import Path
from urllib.parse import urljoin, urlparse
from zoneinfo import ZoneInfo
import xml.etree.ElementTree as ET

import requests
from bs4 import BeautifulSoup


URL_BASE = "https://www.grupoelecnor.com"
URL_NOTICIAS = "https://www.grupoelecnor.com/noticias"

ARCHIVO_RSS = Path("rss.xml")
ZONA_HORARIA = ZoneInfo("Europe/Madrid")
MAX_ARTICULOS = 3000

CABECERAS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/128.0.0.0 Safari/537.36"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;"
        "q=0.9,image/avif,image/webp,*/*;q=0.8"
    ),
    "Accept-Language": "es-ES,es;q=0.9,en;q=0.7",
    "Cache-Control": "no-cache",
    "Referer": URL_BASE + "/",
}


def dentro_del_horario():
    """
    Las ejecuciones manuales funcionan siempre.

    Las ejecuciones programadas funcionan:
    - De lunes a sábado.
    - Desde las 07:00 hasta las 22:59.
    - Según la hora peninsular española.
    """
    evento = os.environ.get("GITHUB_EVENT_NAME", "")

    if evento == "workflow_dispatch":
        print("Ejecución manual: se ignora el límite horario.")
        return True

    ahora = datetime.now(ZONA_HORARIA)
    print(f"Hora española: {ahora:%Y-%m-%d %H:%M:%S %Z}")

    if ahora.weekday() == 6:
        print("Domingo: no se actualiza.")
        return False

    if not 7 <= ahora.hour <= 22:
        print("Fuera del horario permitido: 07:00-22:59.")
        return False

    return True


def limpiar_texto(valor):
    if valor is None:
        return ""

    return " ".join(str(valor).split()).strip()


def normalizar_url(url):
    url = limpiar_texto(url)

    if not url:
        return ""

    return urljoin(URL_BASE, url).split("#")[0]


def es_noticia(url):
    try:
        partes = urlparse(url)
        dominio = partes.netloc.lower()
        ruta = partes.path.rstrip("/")

        if dominio not in ("grupoelecnor.com", "www.grupoelecnor.com"):
            return False

        if not ruta.startswith("/noticias/"):
            return False

        if ruta == "/noticias":
            return False

        return len(ruta.split("/")) >= 3

    except ValueError:
        return False


def descargar_pagina():
    ultimo_error = None

    for intento in range(1, 4):
        try:
            respuesta = requests.get(
                URL_NOTICIAS,
                headers=CABECERAS,
                timeout=45,
                allow_redirects=True,
            )
            respuesta.raise_for_status()

            if not respuesta.text.strip():
                raise RuntimeError("La web se descargó vacía.")

            print(
                f"Página descargada: {len(respuesta.content)} bytes."
            )
            return respuesta.text

        except Exception as error:
            ultimo_error = error
            print(f"Intento {intento}/3 fallido: {error}")

    raise RuntimeError(
        f"No se pudo descargar la página de Elecnor: {ultimo_error}"
    )


def extraer_fecha(texto_enlace, contenedor=None):
    patron = r"\b(\d{1,2})/(\d{1,2})/(\d{4})\b"
    coincidencia = re.search(patron, texto_enlace)

    if not coincidencia and contenedor is not None:
        texto_contenedor = contenedor.get_text(" ", strip=True)
        coincidencia = re.search(patron, texto_contenedor)

    if coincidencia:
        dia, mes, anio = map(int, coincidencia.groups())

        try:
            fecha = datetime(
                anio,
                mes,
                dia,
                12,
                0,
                tzinfo=ZONA_HORARIA,
            )
            return format_datetime(fecha.astimezone(timezone.utc))
        except ValueError:
            pass

    return format_datetime(datetime.now(timezone.utc))


def limpiar_titulo(texto_enlace):
    titulo = limpiar_texto(texto_enlace)

    titulo = re.sub(
        r"^\s*Leer\s+más\s*",
        "",
        titulo,
        flags=re.IGNORECASE,
    )

    titulo = re.sub(
        r"^\s*\d{1,2}/\d{1,2}/\d{4}\s*",
        "",
        titulo,
    )

    titulo = re.sub(
        r"\s*Leer\s+más\s*$",
        "",
        titulo,
        flags=re.IGNORECASE,
    )

    return limpiar_texto(titulo)


def buscar_contenedor(enlace_html):
    contenedor = enlace_html

    for _ in range(7):
        if contenedor.parent is None:
            break

        contenedor = contenedor.parent

        clases = " ".join(contenedor.get("class", []))

        if contenedor.name in ("article", "li"):
            return contenedor

        if any(
            palabra in clases.lower()
            for palabra in (
                "news",
                "noticia",
                "card",
                "item",
                "result",
            )
        ):
            return contenedor

    return enlace_html.parent or enlace_html


def obtener_imagen(contenedor):
    if contenedor is None:
        return ""

    imagen = contenedor.find("img")

    if not imagen:
        return ""

    for atributo in (
        "data-src",
        "data-lazy-src",
        "data-original",
        "src",
    ):
        url = normalizar_url(imagen.get(atributo))

        if url and not url.startswith("data:"):
            return url

    srcset = limpiar_texto(
        imagen.get("data-srcset")
        or imagen.get("srcset")
    )

    if srcset:
        primera = srcset.split(",")[0].strip().split(" ")[0]
        return normalizar_url(primera)

    return ""


def obtener_resumen(contenedor, titulo):
    if contenedor is None:
        return ""

    selectores = [
        ".description",
        ".summary",
        ".excerpt",
        ".text",
        ".content",
        "p",
    ]

    for selector in selectores:
        for elemento in contenedor.select(selector):
            resumen = limpiar_texto(
                elemento.get_text(" ", strip=True)
            )

            if not resumen:
                continue

            if resumen == titulo:
                continue

            if resumen.lower() in ("leer más", "más noticias"):
                continue

            if len(resumen) >= 30:
                return resumen

    return ""


def completar_desde_noticia(sesion, articulo):
    """
    Abre la noticia para obtener entradilla, imagen y fecha cuando
    esos datos no están completos en el listado.
    """
    try:
        respuesta = sesion.get(
            articulo["link"],
            headers=CABECERAS,
            timeout=35,
        )
        respuesta.raise_for_status()

        sopa = BeautifulSoup(respuesta.text, "html.parser")

        if not articulo["image"]:
            imagen_meta = sopa.select_one(
                'meta[property="og:image"], '
                'meta[name="twitter:image"]'
            )

            if imagen_meta:
                articulo["image"] = normalizar_url(
                    imagen_meta.get("content")
                )

        descripcion_meta = sopa.select_one(
            'meta[name="description"], '
            'meta[property="og:description"]'
        )

        resumen = ""

        if descripcion_meta:
            resumen = limpiar_texto(
                descripcion_meta.get("content")
            )

        if not resumen:
            encabezado = sopa.find("h1")

            if encabezado:
                siguiente = encabezado.find_next(
                    ["h2", "p"]
                )

                if siguiente:
                    resumen = limpiar_texto(
                        siguiente.get_text(" ", strip=True)
                    )

        if resumen:
            articulo["description"] = (
                f"<p>{resumen}</p>"
                f'<p><a href="{articulo["link"]}">'
                f"Leer la noticia completa en Grupo Elecnor"
                f"</a></p>"
            )

        fecha_meta = sopa.select_one(
            'meta[property="article:published_time"], '
            'meta[itemprop="datePublished"]'
        )

        if fecha_meta and fecha_meta.get("content"):
            try:
                valor = fecha_meta.get("content")
                fecha = datetime.fromisoformat(
                    valor.replace("Z", "+00:00")
                )

                if fecha.tzinfo is None:
                    fecha = fecha.replace(tzinfo=ZONA_HORARIA)

                articulo["pubDate"] = format_datetime(
                    fecha.astimezone(timezone.utc)
                )
            except (ValueError, TypeError):
                pass

    except Exception as error:
        print(
            f"AVISO: no se pudo completar {articulo['link']}: "
            f"{error}"
        )

    return articulo


def extraer_noticias():
    html = descargar_pagina()
    sopa = BeautifulSoup(html, "html.parser")
    articulos = []
    enlaces_vistos = set()

    for enlace_html in sopa.find_all("a", href=True):
        enlace = normalizar_url(enlace_html.get("href"))

        if not es_noticia(enlace):
            continue

        enlace_limpio = enlace.split("?")[0].rstrip("/")

        if enlace_limpio in enlaces_vistos:
            continue

        texto_enlace = limpiar_texto(
            enlace_html.get_text(" ", strip=True)
        )

        contenedor = buscar_contenedor(enlace_html)
        titulo = limpiar_titulo(texto_enlace)

        if not titulo or titulo.lower() == "leer más":
            encabezado = contenedor.select_one(
                "h1, h2, h3, h4, "
                ".title, .titulo, .news-title"
            )

            if encabezado:
                titulo = limpiar_titulo(
                    encabezado.get_text(" ", strip=True)
                )

        if len(titulo) < 12:
            continue

        fecha = extraer_fecha(texto_enlace, contenedor)
        imagen = obtener_imagen(contenedor)
        resumen = obtener_resumen(contenedor, titulo)

        descripcion = ""

        if resumen:
            descripcion += f"<p>{resumen}</p>"

        descripcion += (
            f'<p><a href="{enlace_limpio}">'
            f"Leer la noticia completa en Grupo Elecnor"
            f"</a></p>"
        )

        articulos.append(
            {
                "title": titulo,
                "link": enlace_limpio,
                "guid": enlace_limpio,
                "pubDate": fecha,
                "description": descripcion,
                "author": "Grupo Elecnor",
                "categories": ["Elecnor", "Noticias corporativas"],
                "image": imagen,
            }
        )

        enlaces_vistos.add(enlace_limpio)

    print(
        f"Noticias únicas encontradas en el listado: "
        f"{len(articulos)}"
    )

    # Solo se completan las noticias más recientes para no hacer
    # demasiadas peticiones en cada ejecución.
    sesion = requests.Session()
    sesion.headers.update(CABECERAS)

    articulos.sort(key=fecha_ordenacion, reverse=True)

    for indice in range(min(15, len(articulos))):
        articulos[indice] = completar_desde_noticia(
            sesion,
            articulos[indice],
        )

    return articulos


def leer_articulos_anteriores():
    if not ARCHIVO_RSS.exists():
        return []

    try:
        raiz = ET.parse(ARCHIVO_RSS).getroot()
    except ET.ParseError:
        print("El rss.xml anterior no es válido; se reconstruirá.")
        return []

    articulos = []

    for item in raiz.findall("./channel/item"):
        categorias = [
            limpiar_texto(elemento.text)
            for elemento in item.findall("category")
            if limpiar_texto(elemento.text)
        ]

        enclosure = item.find("enclosure")
        imagen = ""

        if enclosure is not None:
            imagen = limpiar_texto(enclosure.get("url"))

        articulos.append(
            {
                "title": limpiar_texto(item.findtext("title")),
                "link": limpiar_texto(item.findtext("link")),
                "guid": limpiar_texto(item.findtext("guid")),
                "pubDate": limpiar_texto(item.findtext("pubDate")),
                "description": limpiar_texto(
                    item.findtext("description")
                ),
                "author": limpiar_texto(item.findtext("author")),
                "categories": categorias,
                "image": imagen,
            }
        )

    print(
        f"Noticias recuperadas del RSS anterior: {len(articulos)}"
    )
    return articulos


def clave_articulo(articulo):
    enlace = limpiar_texto(articulo.get("link"))

    if enlace:
        return enlace.split("?")[0].rstrip("/").lower()

    return limpiar_texto(
        articulo.get("guid") or articulo.get("title")
    ).lower()


def fecha_ordenacion(articulo):
    try:
        fecha = parsedate_to_datetime(articulo["pubDate"])

        if fecha.tzinfo is None:
            fecha = fecha.replace(tzinfo=timezone.utc)

        return fecha.timestamp()
    except (TypeError, ValueError, OverflowError, KeyError):
        return 0


def combinar_articulos(nuevos, anteriores):
    resultado = []
    vistos = set()

    nuevos.sort(key=fecha_ordenacion, reverse=True)

    for articulo in nuevos + anteriores:
        clave = clave_articulo(articulo)

        if not clave or clave in vistos:
            continue

        vistos.add(clave)
        resultado.append(articulo)

        if len(resultado) >= MAX_ARTICULOS:
            break

    return resultado


def añadir_texto(padre, etiqueta, valor):
    elemento = ET.SubElement(padre, etiqueta)
    elemento.text = limpiar_texto(valor)
    return elemento


def crear_rss(articulos):
    rss = ET.Element(
        "rss",
        {
            "version": "2.0",
            "xmlns:atom": "http://www.w3.org/2005/Atom",
        },
    )

    canal = ET.SubElement(rss, "channel")

    añadir_texto(canal, "title", "Grupo Elecnor — Noticias")
    añadir_texto(canal, "link", URL_NOTICIAS)
    añadir_texto(
        canal,
        "description",
        "Todas las noticias publicadas por Grupo Elecnor.",
    )
    añadir_texto(canal, "language", "es")
    añadir_texto(
        canal,
        "lastBuildDate",
        format_datetime(datetime.now(timezone.utc)),
    )
    añadir_texto(
        canal,
        "generator",
        "GitHub Actions RSS Generator",
    )

    atom = ET.SubElement(
        canal,
        "{http://www.w3.org/2005/Atom}link",
    )
    atom.set(
        "href",
        (
            "https://raw.githubusercontent.com/"
            "plis2100/elecnor-noticias-rss/main/rss.xml"
        ),
    )
    atom.set("rel", "self")
    atom.set("type", "application/rss+xml")

    for articulo in articulos:
        item = ET.SubElement(canal, "item")

        añadir_texto(item, "title", articulo["title"])
        añadir_texto(item, "link", articulo["link"])

        guid = añadir_texto(item, "guid", articulo["guid"])
        guid.set("isPermaLink", "true")

        añadir_texto(item, "pubDate", articulo["pubDate"])
        añadir_texto(
            item,
            "description",
            articulo["description"],
        )
        añadir_texto(item, "author", articulo["author"])

        for categoria in articulo["categories"]:
            añadir_texto(item, "category", categoria)

        if articulo["image"]:
            enclosure = ET.SubElement(item, "enclosure")
            enclosure.set("url", articulo["image"])
            enclosure.set("type", "image/jpeg")

    arbol = ET.ElementTree(rss)
    ET.indent(arbol, space="  ")

    temporal = ARCHIVO_RSS.with_suffix(".xml.tmp")

    arbol.write(
        temporal,
        encoding="utf-8",
        xml_declaration=True,
    )

    temporal.replace(ARCHIVO_RSS)


def main():
    if not dentro_del_horario():
        return

    nuevos = extraer_noticias()
    anteriores = leer_articulos_anteriores()

    print(f"Noticias nuevas localizadas: {len(nuevos)}")

    if not nuevos and not anteriores:
        raise RuntimeError(
            "No se encontró ninguna noticia y tampoco existe "
            "un RSS anterior."
        )

    if not nuevos and anteriores:
        print(
            "AVISO: no se encontraron noticias nuevas. "
            "Se conservará el RSS anterior."
        )

    articulos = combinar_articulos(nuevos, anteriores)

    if not articulos:
        raise RuntimeError("No hay artículos para escribir en el RSS.")

    crear_rss(articulos)

    print(
        f"RSS creado correctamente con {len(articulos)} noticias."
    )


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(f"ERROR: {error}", file=sys.stderr)
        raise
