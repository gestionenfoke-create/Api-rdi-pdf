from __future__ import annotations

import base64
import html
import json
import os
import re
import traceback
from datetime import datetime
from io import BytesIO
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import requests
from flask import Flask, Response, jsonify, request, send_file
from PIL import Image as PILImage, ImageOps
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_LEFT
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import cm
from reportlab.platypus import (
    Image,
    PageBreak,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

app = Flask(__name__)

APPSHEET_APP_ID = os.environ.get("APPSHEET_APP_ID")
APPSHEET_ACCESS_KEY = os.environ.get("APPSHEET_ACCESS_KEY")
APPSHEET_TABLE = os.environ.get("APPSHEET_TABLE", "Historial RDI")

BASE_DIR = Path(__file__).resolve().parent
LOCAL_LOGO = BASE_DIR / "logo.jpg"

SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "Mozilla/5.0"})


# -----------------------------------------------------------------------------
# Identidad visual del PDF
# -----------------------------------------------------------------------------
# Paleta derivada del logo ENFOKE (naranja corporativo aproximado #CB4D12)
COLOR_PRIMARIO = colors.HexColor("#CB4D12")
COLOR_PRIMARIO_OSCURO = colors.HexColor("#8E3209")
COLOR_SECUNDARIO = colors.HexColor("#E5783A")
COLOR_ACENTO = colors.HexColor("#A63C0B")
COLOR_FONDO = colors.HexColor("#FFF8F4")
COLOR_FONDO_SUAVE = colors.HexColor("#FCEADF")
COLOR_BORDE = colors.HexColor("#E9C6B2")
COLOR_TEXTO = colors.HexColor("#332A26")
COLOR_MUTED = colors.HexColor("#7A655B")
COLOR_BLANCO = colors.white


@app.route("/")
def home():
    return "API RDI funcionando"


@app.route("/health")
def health():
    return {
        "status": "ok",
        "api": "rdi",
        "server_time": datetime.now().isoformat(),
    }


def validar_configuracion() -> None:
    faltantes = []
    if not APPSHEET_APP_ID:
        faltantes.append("APPSHEET_APP_ID")
    if not APPSHEET_ACCESS_KEY:
        faltantes.append("APPSHEET_ACCESS_KEY")

    if faltantes:
        raise RuntimeError(
            "Faltan variables de entorno: " + ", ".join(faltantes)
        )


def escapar_selector(valor: str) -> str:
    return str(valor).replace("\\", "\\\\").replace('"', '\\"')


def appsheet_find_rdi(id_rdi: str) -> list[dict[str, Any]]:
    validar_configuracion()

    url = (
        f"https://api.appsheet.com/api/v2/apps/"
        f"{APPSHEET_APP_ID}/tables/{APPSHEET_TABLE}/Action"
    )

    headers = {
        "ApplicationAccessKey": APPSHEET_ACCESS_KEY,
        "Content-Type": "application/json",
        "Accept": "application/json",
    }

    id_seguro = escapar_selector(id_rdi)

    payload = {
        "Action": "Find",
        "Properties": {
            "Locale": "es-CL",
            "Location": "0,0",
            "Timezone": "America/Santiago",
            "Selector": (
                f'FILTER("{APPSHEET_TABLE}", '
                f'[ID_RDI] = "{id_seguro}")'
            ),
        },
        "Rows": [{}],
    }

    response = SESSION.post(
        url,
        headers=headers,
        json=payload,
        timeout=90,
    )

    if response.status_code != 200:
        raise RuntimeError(
            f"Error AppSheet {APPSHEET_TABLE}: "
            f"{response.status_code} - {response.text}"
        )

    data = response.json()
    return data if isinstance(data, list) else []


def appsheet_file_url(file_path: str) -> str | None:
    if not file_path:
        return None

    response = requests.Request(
        "GET",
        "https://www.appsheet.com/template/gettablefileurl",
        params={
            "appName": APPSHEET_APP_ID,
            "tableName": APPSHEET_TABLE,
            "fileName": file_path,
        },
    ).prepare()

    return response.url


def descargar_imagen(
    file_path: str,
    max_width: float = 8.0 * cm,
    max_height: float = 7.0 * cm,
) -> Image | None:
    url = appsheet_file_url(file_path)
    if not url:
        return None

    try:
        response = SESSION.get(
            url,
            headers={"Accept": "image/*"},
            timeout=30,
        )

        if response.status_code != 200:
            print("Imagen no descargada:", response.status_code, file_path)
            return None

        content_type = response.headers.get("Content-Type", "").lower()
        if "image" not in content_type:
            print("Archivo no reconocido como imagen:", content_type, file_path)
            return None

        pil_img = PILImage.open(BytesIO(response.content))
        pil_img = ImageOps.exif_transpose(pil_img).convert("RGB")
        pil_img.thumbnail((1600, 1200))

        buffer = BytesIO()
        pil_img.save(
            buffer,
            format="JPEG",
            quality=85,
            optimize=True,
            progressive=True,
        )
        buffer.seek(0)

        width_px, height_px = pil_img.size
        ratio = min(max_width / width_px, max_height / height_px)

        return Image(
            buffer,
            width=width_px * ratio,
            height=height_px * ratio,
        )

    except Exception as exc:
        print("ERROR IMAGEN:", file_path, str(exc))
        return None


def formatear_fecha(valor: Any) -> str:
    texto = str(valor or "").strip()
    if not texto:
        return ""

    formatos = (
        "%d/%m/%Y",
        "%m/%d/%Y",
        "%Y-%m-%d",
        "%Y-%m-%dT%H:%M:%S",
    )

    for formato in formatos:
        try:
            return datetime.strptime(texto[:19], formato).strftime("%d/%m/%Y")
        except ValueError:
            continue

    return texto


def texto_seguro(valor: Any) -> str:
    texto = str(valor or "").strip()
    return (
        texto.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace("\n", "<br/>")
    )


def nombre_archivo_seguro(valor: str) -> str:
    valor = re.sub(r'[\\/:*?"<>|]+', "-", valor)
    valor = re.sub(r"\s+", "_", valor.strip())
    return valor or "RDI"


def _p(valor: Any, estilo: ParagraphStyle) -> Paragraph:
    return Paragraph(texto_seguro(valor), estilo)


def agregar_logo(story: list[Any]) -> None:
    """Mantiene compatibilidad con la función histórica."""
    if LOCAL_LOGO.exists():
        logo = Image(str(LOCAL_LOGO), width=5.2 * cm, height=1.42 * cm)
        logo.hAlign = "LEFT"
        story.append(logo)


def obtener_logo_para_tabla() -> Any:
    if LOCAL_LOGO.exists():
        return Image(str(LOCAL_LOGO), width=5.2 * cm, height=1.42 * cm)
    return ""


def agregar_titulo_seccion(
    story: list[Any],
    titulo: str,
    styles: dict[str, ParagraphStyle],
    color: colors.Color = COLOR_PRIMARIO,
) -> None:
    barra = Table(
        [[Paragraph(texto_seguro(titulo).upper(), styles["section_bar"]) ]],
        colWidths=[17.6 * cm],
    )
    barra.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, -1), color),
                ("BOX", (0, 0), (-1, -1), 0.5, color),
                ("LEFTPADDING", (0, 0), (-1, -1), 9),
                ("RIGHTPADDING", (0, 0), (-1, -1), 9),
                ("TOPPADDING", (0, 0), (-1, -1), 6),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
            ]
        )
    )
    story.append(Spacer(1, 0.24 * cm))
    story.append(barra)
    story.append(Spacer(1, 0.22 * cm))


def agregar_bloque_texto(
    story: list[Any],
    contenido: Any,
    styles: dict[str, ParagraphStyle],
) -> None:
    texto = texto_seguro(contenido) or "Sin información registrada."
    caja = Table(
        [[Paragraph(texto, styles["normal"]) ]],
        colWidths=[17.6 * cm],
    )
    caja.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, -1), COLOR_FONDO),
                ("BOX", (0, 0), (-1, -1), 0.7, COLOR_BORDE),
                ("LEFTPADDING", (0, 0), (-1, -1), 12),
                ("RIGHTPADDING", (0, 0), (-1, -1), 12),
                ("TOPPADDING", (0, 0), (-1, -1), 10),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 10),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ]
        )
    )
    story.append(caja)


def agregar_imagenes(
    story: list[Any],
    registro: dict[str, Any],
    columnas: list[str],
    titulo: str,
    styles: dict[str, ParagraphStyle],
) -> None:
    imagenes = []

    for columna in columnas:
        file_path = str(registro.get(columna, "") or "").strip()
        if not file_path:
            continue

        imagen = descargar_imagen(
            file_path,
            max_width=8.1 * cm,
            max_height=6.4 * cm,
        )
        if imagen:
            imagenes.append(imagen)

    agregar_titulo_seccion(story, titulo, styles, COLOR_SECUNDARIO)

    if not imagenes:
        caja = Table(
            [[Paragraph("No se adjuntaron imágenes.", styles["small_center"]) ]],
            colWidths=[17.6 * cm],
        )
        caja.setStyle(
            TableStyle(
                [
                    ("BACKGROUND", (0, 0), (-1, -1), COLOR_FONDO),
                    ("BOX", (0, 0), (-1, -1), 0.6, COLOR_BORDE),
                    ("TOPPADDING", (0, 0), (-1, -1), 9),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 9),
                ]
            )
        )
        story.append(caja)
        return

    filas = []
    for indice in range(0, len(imagenes), 2):
        izquierda = imagenes[indice]
        derecha = imagenes[indice + 1] if indice + 1 < len(imagenes) else ""
        filas.append([izquierda, derecha])

    tabla = Table(
        filas,
        colWidths=[8.65 * cm, 8.65 * cm],
        hAlign="CENTER",
    )
    tabla.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, -1), COLOR_BLANCO),
                ("BOX", (0, 0), (-1, -1), 0.6, COLOR_BORDE),
                ("INNERGRID", (0, 0), (-1, -1), 0.35, COLOR_BORDE),
                ("ALIGN", (0, 0), (-1, -1), "CENTER"),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("LEFTPADDING", (0, 0), (-1, -1), 7),
                ("RIGHTPADDING", (0, 0), (-1, -1), 7),
                ("TOPPADDING", (0, 0), (-1, -1), 8),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
            ]
        )
    )
    story.append(tabla)


def agregar_archivos_adjuntos(
    story: list[Any],
    registro: dict[str, Any],
    columnas: list[str],
    styles: dict[str, ParagraphStyle],
) -> None:
    """Agrega una sección visual de archivos con enlaces clicables."""
    archivos: list[tuple[str, str, str]] = []

    for columna in columnas:
        ruta = str(registro.get(columna, "") or "").strip()
        if not ruta:
            continue

        url = appsheet_file_url(ruta)
        if not url:
            continue

        nombre = Path(urlparse(ruta).path).name or ruta
        archivos.append((columna, nombre, url))

    if not archivos:
        return

    agregar_titulo_seccion(story, "Archivos adjuntos", styles, COLOR_SECUNDARIO)

    filas = []
    for etiqueta, nombre, url in archivos:
        url_pdf = texto_seguro(url)
        etiqueta_pdf = texto_seguro(etiqueta)
        nombre_pdf = texto_seguro(nombre)
        filas.append(
            [
                Paragraph(f"<b>{etiqueta_pdf}</b>", styles["attachment_label"]),
                Paragraph(
                    f'<link href="{url_pdf}" color="#CB4D12">'
                    f'<b><u>Abrir {nombre_pdf}</u></b></link>',
                    styles["attachment_link"],
                ),
            ]
        )

    tabla = Table(filas, colWidths=[4.1 * cm, 13.5 * cm])
    tabla.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, -1), COLOR_FONDO),
                ("BOX", (0, 0), (-1, -1), 0.6, COLOR_BORDE),
                ("INNERGRID", (0, 0), (-1, -1), 0.35, COLOR_BORDE),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("LEFTPADDING", (0, 0), (-1, -1), 9),
                ("RIGHTPADDING", (0, 0), (-1, -1), 9),
                ("TOPPADDING", (0, 0), (-1, -1), 7),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
            ]
        )
    )
    story.append(tabla)


def construir_estilos() -> dict[str, ParagraphStyle]:
    base = getSampleStyleSheet()

    return {
        "eyebrow": ParagraphStyle(
            "Eyebrow",
            parent=base["Normal"],
            fontName="Helvetica-Bold",
            fontSize=8.2,
            leading=10,
            textColor=COLOR_MUTED,
            spaceAfter=2,
        ),
        "title": ParagraphStyle(
            "RDITitle",
            parent=base["Title"],
            fontName="Helvetica-Bold",
            fontSize=20,
            leading=23,
            textColor=COLOR_PRIMARIO,
            alignment=TA_LEFT,
            spaceAfter=2,
        ),
        "subtitle": ParagraphStyle(
            "RDISubtitle",
            parent=base["Normal"],
            fontName="Helvetica-Bold",
            fontSize=9,
            leading=11,
            textColor=COLOR_ACENTO,
            alignment=TA_LEFT,
        ),
        "project_label": ParagraphStyle(
            "ProjectLabel",
            parent=base["Normal"],
            fontName="Helvetica-Bold",
            fontSize=8.2,
            leading=10,
            textColor=COLOR_MUTED,
            spaceAfter=2,
        ),
        "project": ParagraphStyle(
            "Project",
            parent=base["Heading1"],
            fontName="Helvetica-Bold",
            fontSize=14,
            leading=17,
            textColor=COLOR_TEXTO,
            spaceAfter=0,
        ),
        "meta_label": ParagraphStyle(
            "MetaLabel",
            parent=base["Normal"],
            fontName="Helvetica-Bold",
            fontSize=7.6,
            leading=9,
            textColor=COLOR_MUTED,
            alignment=TA_LEFT,
        ),
        "meta_value": ParagraphStyle(
            "MetaValue",
            parent=base["Normal"],
            fontName="Helvetica-Bold",
            fontSize=9.3,
            leading=11,
            textColor=COLOR_TEXTO,
            alignment=TA_LEFT,
        ),
        "normal": ParagraphStyle(
            "RDINormal",
            parent=base["Normal"],
            fontName="Helvetica",
            fontSize=10,
            leading=14.3,
            textColor=COLOR_TEXTO,
            spaceAfter=0,
        ),
        "section_bar": ParagraphStyle(
            "SectionBar",
            parent=base["Heading2"],
            fontName="Helvetica-Bold",
            fontSize=9.3,
            leading=11,
            textColor=COLOR_BLANCO,
            alignment=TA_LEFT,
            spaceBefore=0,
            spaceAfter=0,
        ),
        "small": ParagraphStyle(
            "Small",
            parent=base["Normal"],
            fontName="Helvetica",
            fontSize=8.4,
            leading=10.5,
            textColor=COLOR_MUTED,
        ),
        "small_center": ParagraphStyle(
            "SmallCenter",
            parent=base["Normal"],
            fontName="Helvetica",
            fontSize=8.4,
            leading=10.5,
            alignment=TA_CENTER,
            textColor=COLOR_MUTED,
        ),
        "author": ParagraphStyle(
            "Author",
            parent=base["Normal"],
            fontName="Helvetica-Bold",
            fontSize=8.8,
            leading=11,
            textColor=COLOR_PRIMARIO,
        ),
        "attachment_label": ParagraphStyle(
            "AttachmentLabel",
            parent=base["Normal"],
            fontName="Helvetica-Bold",
            fontSize=8.4,
            leading=10,
            textColor=COLOR_MUTED,
        ),
        "attachment_link": ParagraphStyle(
            "AttachmentLink",
            parent=base["Normal"],
            fontName="Helvetica",
            fontSize=9,
            leading=11,
            textColor=COLOR_SECUNDARIO,
        ),
    }


def agregar_encabezado_rdi(
    story: list[Any],
    registro: dict[str, Any],
    styles: dict[str, ParagraphStyle],
    fecha_etiqueta: str,
    fecha_columna: str,
    subtitulo: str = "",
) -> None:
    numero = registro.get("N RDI", "")
    proyecto = registro.get("PROYECTO", "")
    especialidad = registro.get("ESPECIALIDAD", "")
    sector = registro.get("SECTOR", "")
    fecha = formatear_fecha(registro.get(fecha_columna, "")) or "No registrada"

    logo = obtener_logo_para_tabla()
    etapa = "RESPUESTA" if subtitulo else "EMISIÓN"

    titulo = Paragraph(
        f"<font size='8' color='#7A655B'><b>REQUERIMIENTO DE INFORMACIÓN</b></font>"
        f"<br/><font size='3'> </font><br/>"
        f"<font size='20' color='#8E3209'><b>RDI N° {texto_seguro(numero)}</b></font><br/>"
        f"<font size='8.5' color='#CB4D12'><b>{etapa}</b></font>",
        styles["normal"],
    )

    cabecera = Table(
        [[logo, titulo]],
        colWidths=[5.7 * cm, 11.9 * cm],
        rowHeights=[1.58 * cm],
    )
    cabecera.setStyle(
        TableStyle(
            [
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("ALIGN", (0, 0), (0, 0), "LEFT"),
                ("ALIGN", (1, 0), (1, 0), "RIGHT"),
                ("LEFTPADDING", (0, 0), (-1, -1), 0),
                ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                ("TOPPADDING", (0, 0), (-1, -1), 0),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
                ("LINEBELOW", (0, 0), (-1, -1), 1.2, COLOR_PRIMARIO_OSCURO),
            ]
        )
    )
    story.append(cabecera)
    story.append(Spacer(1, 0.32 * cm))

    proyecto_label_style = ParagraphStyle(
        "ProjectLabelWhite",
        parent=styles["project_label"],
        textColor=COLOR_BLANCO,
        alignment=TA_CENTER,
    )
    proyecto_caja = Table(
        [[
            Paragraph("PROYECTO", proyecto_label_style),
            Paragraph(texto_seguro(proyecto) or "Sin proyecto", styles["project"]),
        ]],
        colWidths=[3.0 * cm, 14.6 * cm],
    )
    proyecto_caja.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (0, 0), COLOR_PRIMARIO),
                ("BACKGROUND", (1, 0), (1, 0), COLOR_FONDO_SUAVE),
                ("BOX", (0, 0), (-1, -1), 0.7, COLOR_BORDE),
                ("LINEAFTER", (0, 0), (0, 0), 0.7, COLOR_BORDE),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("LEFTPADDING", (0, 0), (-1, -1), 10),
                ("RIGHTPADDING", (0, 0), (-1, -1), 10),
                ("TOPPADDING", (0, 0), (-1, -1), 8),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
            ]
        )
    )
    story.append(proyecto_caja)
    story.append(Spacer(1, 0.22 * cm))

    meta = [
        [
            Paragraph(fecha_etiqueta.upper(), styles["meta_label"]),
            Paragraph("ESPECIALIDAD", styles["meta_label"]),
            Paragraph("SECTOR", styles["meta_label"]),
        ],
        [
            Paragraph(texto_seguro(fecha), styles["meta_value"]),
            Paragraph(texto_seguro(especialidad) or "-", styles["meta_value"]),
            Paragraph(texto_seguro(sector) or "-", styles["meta_value"]),
        ],
    ]
    tabla_meta = Table(meta, colWidths=[5.15 * cm, 5.6 * cm, 6.85 * cm])
    tabla_meta.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), COLOR_FONDO),
                ("BACKGROUND", (0, 1), (-1, 1), COLOR_BLANCO),
                ("BOX", (0, 0), (-1, -1), 0.7, COLOR_BORDE),
                ("INNERGRID", (0, 0), (-1, -1), 0.35, COLOR_BORDE),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 8),
                ("RIGHTPADDING", (0, 0), (-1, -1), 8),
                ("TOPPADDING", (0, 0), (-1, -1), 6),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
            ]
        )
    )
    story.append(tabla_meta)
    story.append(Spacer(1, 0.18 * cm))


def agregar_firma_informativa(
    story: list[Any],
    etiqueta: str,
    nombre: Any,
    styles: dict[str, ParagraphStyle],
) -> None:
    if not nombre:
        return

    story.append(Spacer(1, 0.35 * cm))
    bloque = Table(
        [[
            Paragraph(texto_seguro(etiqueta).upper(), styles["meta_label"]),
            Paragraph(texto_seguro(nombre), styles["author"]),
        ]],
        colWidths=[4.8 * cm, 12.8 * cm],
    )
    bloque.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, -1), COLOR_FONDO_SUAVE),
                ("BOX", (0, 0), (-1, -1), 0.6, COLOR_BORDE),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("LEFTPADDING", (0, 0), (-1, -1), 9),
                ("RIGHTPADDING", (0, 0), (-1, -1), 9),
                ("TOPPADDING", (0, 0), (-1, -1), 7),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
            ]
        )
    )
    story.append(bloque)


def crear_pdf_rdi(registro: dict[str, Any]) -> BytesIO:
    buffer = BytesIO()

    doc = SimpleDocTemplate(
        buffer,
        pagesize=letter,
        rightMargin=1.35 * cm,
        leftMargin=1.35 * cm,
        topMargin=0.95 * cm,
        bottomMargin=1.45 * cm,
        title=f"RDI {registro.get('N RDI', '')}",
        author=str(registro.get("EMITIDO POR", "") or ""),
        subject="Requerimiento de Información",
    )

    styles = construir_estilos()
    story: list[Any] = []

    # Página 1: emisión y explicación
    agregar_encabezado_rdi(
        story,
        registro,
        styles,
        "Fecha Emisión RDI",
        "FECHA ENVIO",
    )

    agregar_titulo_seccion(story, "Explicación de la RDI", styles)
    agregar_bloque_texto(
        story,
        registro.get("EXPLICACION DE LA RDI", ""),
        styles,
    )

    agregar_imagenes(
        story,
        registro,
        ["IMAGEN 1 P", "IMAGEN 2 P"],
        "Imágenes explicativas",
        styles,
    )

    agregar_archivos_adjuntos(
        story,
        registro,
        ["ARCHIVO 1 P", "ARCHIVO 2 P"],
        styles,
    )

    agregar_firma_informativa(
        story,
        "Emitido por",
        registro.get("EMITIDO POR", ""),
        styles,
    )

    # Página 2: respuesta
    story.append(PageBreak())

    agregar_encabezado_rdi(
        story,
        registro,
        styles,
        "Fecha Respuesta RDI",
        "FECHA RESPUESTA",
        "Respuesta",
    )

    agregar_titulo_seccion(story, "Respuesta de la RDI", styles, COLOR_PRIMARIO_OSCURO)
    agregar_bloque_texto(
        story,
        registro.get("RESPUESTA DE LA RDI", ""),
        styles,
    )

    agregar_imagenes(
        story,
        registro,
        ["IMAGEN 1 R", "IMAGEN 2 R", "IMAGEN 3 R", "IMAGEN 4 R"],
        "Imágenes de la respuesta",
        styles,
    )

    agregar_archivos_adjuntos(
        story,
        registro,
        ["ARCHIVO 1 R", "ARCHIVO 2 R", "Video R."],
        styles,
    )

    agregar_firma_informativa(
        story,
        "Respuesta entregada por",
        registro.get("RESPUESTA DE", ""),
        styles,
    )

    numero = texto_seguro(registro.get("N RDI", ""))
    proyecto = str(registro.get("PROYECTO", "") or "").strip()

    def decorar_pagina(canvas, doc_obj) -> None:
        canvas.saveState()
        ancho, alto = letter

        # Franja superior discreta de identidad.
        canvas.setFillColor(COLOR_PRIMARIO)
        canvas.rect(0, alto - 0.16 * cm, ancho, 0.16 * cm, fill=1, stroke=0)
        canvas.setFillColor(COLOR_PRIMARIO_OSCURO)
        canvas.rect(0, alto - 0.20 * cm, ancho, 0.04 * cm, fill=1, stroke=0)

        # Pie de página.
        y = 0.72 * cm
        canvas.setStrokeColor(COLOR_BORDE)
        canvas.setLineWidth(0.5)
        canvas.line(1.35 * cm, y + 0.22 * cm, ancho - 1.35 * cm, y + 0.22 * cm)

        canvas.setFont("Helvetica", 7.5)
        canvas.setFillColor(COLOR_MUTED)
        izquierda = proyecto[:55] if proyecto else "Requerimiento de Información"
        canvas.drawString(1.35 * cm, y - 0.02 * cm, izquierda)

        canvas.drawCentredString(
            ancho / 2,
            y - 0.02 * cm,
            f"RDI N° {numero}",
        )
        canvas.drawRightString(
            ancho - 1.35 * cm,
            y - 0.02 * cm,
            f"Página {doc_obj.page}",
        )
        canvas.restoreState()

    doc.build(
        story,
        onFirstPage=decorar_pagina,
        onLaterPages=decorar_pagina,
    )
    buffer.seek(0)
    return buffer


@app.route("/test_appsheet")
def test_appsheet():
    try:
        id_rdi = request.args.get("id", "").strip()
        if not id_rdi:
            return {"error": "Falta parámetro id"}, 400

        rows = appsheet_find_rdi(id_rdi)

        return jsonify(
            {
                "id_rdi": id_rdi,
                "registros_encontrados": len(rows),
                "registro": rows[0] if rows else None,
            }
        )

    except Exception as exc:
        traceback.print_exc()
        return {"error": str(exc)}, 500


def logo_data_uri() -> str:
    """Devuelve logo.jpg embebido para la pantalla de compartir."""
    if not LOCAL_LOGO.exists():
        return ""

    try:
        contenido = LOCAL_LOGO.read_bytes()
        encoded = base64.b64encode(contenido).decode("ascii")
        return f"data:image/jpeg;base64,{encoded}"
    except Exception:
        traceback.print_exc()
        return ""


@app.route("/compartir")
def compartir():
    """
    Pantalla intermedia para compartir la RDI como archivo PDF usando
    el menú nativo del dispositivo (WhatsApp, correo, Teams, etc.).

    Uso:
        /compartir?id=<ID_RDI>
    """
    id_rdi = request.args.get("id", "").strip()
    if not id_rdi:
        return {"error": "Falta parámetro id"}, 400

    id_html = html.escape(id_rdi)
    id_js = json.dumps(id_rdi)
    logo_uri = logo_data_uri()
    logo_html = (
        f'<img class="logo" src="{logo_uri}" alt="ENFOKE">'
        if logo_uri
        else '<div class="marca-texto">ENFOKE</div>'
    )

    pagina = f"""<!doctype html>
<html lang="es">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
  <meta name="theme-color" content="#CB4D12">
  <title>Compartir RDI</title>
  <style>
    :root {{
      --primario: #CB4D12;
      --oscuro: #8E3209;
      --suave: #FCEADF;
      --fondo: #FFF8F4;
      --texto: #332A26;
      --muted: #7A655B;
      --borde: #E9C6B2;
      --blanco: #FFFFFF;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      min-height: 100vh;
      font-family: Arial, Helvetica, sans-serif;
      background: linear-gradient(180deg, var(--fondo) 0%, #ffffff 58%);
      color: var(--texto);
      display: flex;
      align-items: center;
      justify-content: center;
      padding: 24px;
    }}
    .card {{
      width: min(540px, 100%);
      background: var(--blanco);
      border: 1px solid var(--borde);
      border-radius: 18px;
      box-shadow: 0 18px 46px rgba(76, 38, 20, .14);
      overflow: hidden;
    }}
    .topbar {{ height: 8px; background: var(--primario); }}
    .content {{ padding: 30px; }}
    .brand {{ text-align: center; margin-bottom: 24px; }}
    .logo {{
      display: block;
      width: min(340px, 86%);
      height: auto;
      margin: 0 auto;
      border-radius: 8px;
    }}
    .marca-texto {{
      color: var(--primario);
      font-weight: 700;
      letter-spacing: .35em;
      font-size: 26px;
    }}
    .eyebrow {{
      margin: 0 0 8px;
      text-transform: uppercase;
      letter-spacing: .08em;
      color: var(--primario);
      font-size: 12px;
      font-weight: 700;
    }}
    h1 {{
      margin: 0;
      color: var(--oscuro);
      font-size: clamp(26px, 7vw, 36px);
      line-height: 1.05;
    }}
    .id {{
      display: inline-block;
      margin-top: 12px;
      padding: 7px 11px;
      border-radius: 999px;
      background: var(--suave);
      color: var(--oscuro);
      font-size: 13px;
      font-weight: 700;
      word-break: break-all;
    }}
    .estado {{
      margin: 24px 0 18px;
      padding: 14px 16px;
      border: 1px solid var(--borde);
      background: var(--fondo);
      border-radius: 12px;
      font-size: 14px;
      line-height: 1.45;
    }}
    .estado strong {{ color: var(--oscuro); }}
    .acciones {{ display: grid; gap: 11px; }}
    button, .btn {{
      appearance: none;
      width: 100%;
      border-radius: 11px;
      padding: 14px 18px;
      font-size: 15px;
      font-weight: 700;
      cursor: pointer;
      text-decoration: none;
      text-align: center;
      transition: transform .12s ease, opacity .12s ease;
    }}
    button:active, .btn:active {{ transform: scale(.99); }}
    #shareBtn {{
      border: 0;
      color: var(--blanco);
      background: var(--primario);
    }}
    #shareBtn:disabled {{ opacity: .5; cursor: wait; }}
    #downloadBtn {{
      display: none;
      border: 1px solid var(--primario);
      color: var(--primario);
      background: var(--blanco);
    }}
    .nota {{
      margin: 18px 0 0;
      color: var(--muted);
      font-size: 12px;
      line-height: 1.45;
      text-align: center;
    }}
    .error {{ color: #9f1f16; background: #fff0ee; border-color: #f0bbb6; }}
    .ok {{ color: #245c35; background: #eef8f0; border-color: #bfdec7; }}
  </style>
</head>
<body>
  <main class="card">
    <div class="topbar"></div>
    <div class="content">
      <div class="brand">{logo_html}</div>
      <p class="eyebrow">Requerimiento de Información</p>
      <h1>Compartir RDI</h1>
      <div class="id">ID: {id_html}</div>

      <div id="estado" class="estado">
        <strong>Preparando PDF...</strong><br>
        El documento se está generando para poder compartirlo como archivo.
      </div>

      <div class="acciones">
        <button id="shareBtn" type="button" disabled>Compartir PDF</button>
        <a id="downloadBtn" class="btn" href="#" download>Descargar PDF</a>
      </div>

      <p class="nota">
        Al compartir, tu dispositivo mostrará las aplicaciones disponibles.
        Puedes elegir WhatsApp y luego seleccionar el contacto o grupo.
      </p>
    </div>
  </main>

  <script>
    const idRdi = {id_js};
    const estado = document.getElementById('estado');
    const shareBtn = document.getElementById('shareBtn');
    const downloadBtn = document.getElementById('downloadBtn');

    let pdfFile = null;
    let objectUrl = null;

    function extraerNombreArchivo(response) {{
      const disposition = response.headers.get('Content-Disposition') || '';
      const utf8 = disposition.match(/filename\\*=UTF-8''([^;]+)/i);
      if (utf8 && utf8[1]) {{
        try {{ return decodeURIComponent(utf8[1]); }} catch (_) {{}}
      }}
      const normal = disposition.match(/filename="?([^";]+)"?/i);
      if (normal && normal[1]) return normal[1];
      return `RDI_${{idRdi}}.pdf`;
    }}

    async function prepararPdf() {{
      try {{
        const url = `/generar?id=${{encodeURIComponent(idRdi)}}`;
        const response = await fetch(url, {{ cache: 'no-store' }});
        if (!response.ok) {{
          let detalle = `Error ${{response.status}}`;
          try {{
            const data = await response.json();
            detalle = data.error || detalle;
          }} catch (_) {{}}
          throw new Error(detalle);
        }}

        const blob = await response.blob();
        const filename = extraerNombreArchivo(response);
        pdfFile = new File([blob], filename, {{ type: 'application/pdf' }});
        objectUrl = URL.createObjectURL(blob);

        downloadBtn.href = objectUrl;
        downloadBtn.download = filename;
        downloadBtn.style.display = 'block';

        const puedeCompartirArchivo = Boolean(
          navigator.share &&
          navigator.canShare &&
          navigator.canShare({{ files: [pdfFile] }})
        );

        shareBtn.disabled = false;
        if (puedeCompartirArchivo) {{
          estado.classList.add('ok');
          estado.innerHTML = `<strong>PDF listo.</strong><br>${{filename}} está preparado para compartir.`;
        }} else {{
          estado.innerHTML = '<strong>PDF listo.</strong><br>Este navegador no permite compartir archivos directamente. Puedes descargarlo con el botón inferior.';
          shareBtn.textContent = 'Descargar PDF';
        }}
      }} catch (error) {{
        console.error(error);
        estado.classList.add('error');
        estado.innerHTML = `<strong>No fue posible preparar el PDF.</strong><br>${{error.message}}`;
        shareBtn.disabled = true;
      }}
    }}

    shareBtn.addEventListener('click', async () => {{
      if (!pdfFile) return;

      const puedeCompartirArchivo = Boolean(
        navigator.share &&
        navigator.canShare &&
        navigator.canShare({{ files: [pdfFile] }})
      );

      if (!puedeCompartirArchivo) {{
        downloadBtn.click();
        return;
      }}

      try {{
        await navigator.share({{
          files: [pdfFile],
          title: 'RDI',
          text: 'Requerimiento de Información'
        }});
      }} catch (error) {{
        if (error && error.name === 'AbortError') return;
        console.error(error);
        estado.classList.remove('ok');
        estado.classList.add('error');
        estado.innerHTML = '<strong>No se pudo abrir el menú de compartir.</strong><br>Puedes descargar el PDF y adjuntarlo manualmente.';
      }}
    }});

    window.addEventListener('beforeunload', () => {{
      if (objectUrl) URL.revokeObjectURL(objectUrl);
    }});

    prepararPdf();
  </script>
</body>
</html>"""

    return Response(pagina, mimetype="text/html")


@app.route("/generar")
def generar():
    try:
        id_rdi = request.args.get("id", "").strip()
        if not id_rdi:
            return {"error": "Falta parámetro id"}, 400

        rows = appsheet_find_rdi(id_rdi)
        if not rows:
            return {"error": "RDI no encontrada"}, 404

        registro = rows[0]
        pdf_buffer = crear_pdf_rdi(registro)

        numero = nombre_archivo_seguro(str(registro.get("N RDI", id_rdi)))
        filename = f"RDI_{numero}.pdf"

        return send_file(
            pdf_buffer,
            mimetype="application/pdf",
            download_name=filename,
            as_attachment=False,
        )

    except Exception as exc:
        traceback.print_exc()
        return {"error": str(exc)}, 500


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8080"))
    app.run(host="0.0.0.0", port=port)
