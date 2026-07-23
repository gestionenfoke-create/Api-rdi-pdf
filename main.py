from __future__ import annotations

import os
import re
import traceback
from datetime import datetime
from io import BytesIO
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import requests
from flask import Flask, jsonify, request, send_file
from PIL import Image as PILImage, ImageOps
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER
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


def agregar_logo(story: list[Any]) -> None:
    if LOCAL_LOGO.exists():
        logo = Image(str(LOCAL_LOGO), width=5.2 * cm, height=1.45 * cm)
        logo.hAlign = "CENTER"
        story.append(logo)
        story.append(Spacer(1, 0.55 * cm))


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

        imagen = descargar_imagen(file_path)
        if imagen:
            imagenes.append(imagen)

    story.append(Spacer(1, 0.35 * cm))
    story.append(Paragraph(titulo, styles["section_center"]))
    story.append(Spacer(1, 0.25 * cm))

    if not imagenes:
        story.append(
            Paragraph(
                "No se adjuntaron imágenes.",
                styles["small_center"],
            )
        )
        return

    filas = []
    for indice in range(0, len(imagenes), 2):
        izquierda = imagenes[indice]
        derecha = imagenes[indice + 1] if indice + 1 < len(imagenes) else ""
        filas.append([izquierda, derecha])

    tabla = Table(filas, colWidths=[8.7 * cm, 8.7 * cm])
    tabla.setStyle(
        TableStyle(
            [
                ("ALIGN", (0, 0), (-1, -1), "CENTER"),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("LEFTPADDING", (0, 0), (-1, -1), 4),
                ("RIGHTPADDING", (0, 0), (-1, -1), 4),
                ("TOPPADDING", (0, 0), (-1, -1), 5),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
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
    archivos = []

    for columna in columnas:
        ruta = str(registro.get(columna, "") or "").strip()
        if ruta:
            archivos.append((columna, ruta))

    if not archivos:
        return

    story.append(Spacer(1, 0.3 * cm))
    story.append(Paragraph("Archivos adjuntos", styles["section_center"]))

    for etiqueta, ruta in archivos:
        nombre = Path(urlparse(ruta).path).name or ruta
        story.append(
            Paragraph(
                f"<b>{texto_seguro(etiqueta)}:</b> {texto_seguro(nombre)}",
                styles["small"],
            )
        )


def construir_estilos() -> dict[str, ParagraphStyle]:
    base = getSampleStyleSheet()

    return {
        "title": ParagraphStyle(
            "RDITitle",
            parent=base["Title"],
            fontName="Helvetica-Bold",
            fontSize=17,
            leading=20,
            alignment=TA_CENTER,
            spaceAfter=12,
        ),
        "project_label": ParagraphStyle(
            "ProjectLabel",
            parent=base["Heading2"],
            fontName="Helvetica-Bold",
            fontSize=14,
            alignment=TA_CENTER,
            spaceAfter=8,
        ),
        "project": ParagraphStyle(
            "Project",
            parent=base["Heading1"],
            fontName="Helvetica-Bold",
            fontSize=18,
            leading=22,
            alignment=TA_CENTER,
            spaceAfter=18,
        ),
        "normal": ParagraphStyle(
            "RDINormal",
            parent=base["Normal"],
            fontName="Helvetica",
            fontSize=10.5,
            leading=14,
            spaceAfter=6,
        ),
        "section_center": ParagraphStyle(
            "SectionCenter",
            parent=base["Heading2"],
            fontName="Helvetica-Bold",
            fontSize=12.5,
            leading=15,
            alignment=TA_CENTER,
            spaceBefore=6,
            spaceAfter=8,
        ),
        "small": ParagraphStyle(
            "Small",
            parent=base["Normal"],
            fontSize=8.5,
            leading=11,
        ),
        "small_center": ParagraphStyle(
            "SmallCenter",
            parent=base["Normal"],
            fontSize=8.5,
            leading=11,
            alignment=TA_CENTER,
            textColor=colors.grey,
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
    fecha = formatear_fecha(registro.get(fecha_columna, ""))

    agregar_logo(story)

    if subtitulo:
        story.append(
            Paragraph(subtitulo, styles["project_label"])
        )
        story.append(Spacer(1, 0.25 * cm))
    
    story.append(
        Paragraph(
            f"Requerimiento de Información N°{texto_seguro(numero)}",
            styles["title"],
        )
    )
    story.append(Paragraph("Proyecto", styles["project_label"]))
    story.append(Paragraph(texto_seguro(proyecto), styles["project"]))

    datos = [
        [f"{fecha_etiqueta}:", fecha],
        ["Especialidad:", str(especialidad or "")],
        ["Sector:", str(sector or "")],
    ]

    tabla = Table(datos, colWidths=[4.3 * cm, 12.5 * cm])
    tabla.setStyle(
        TableStyle(
            [
                ("FONTNAME", (0, 0), (0, -1), "Helvetica"),
                ("FONTNAME", (1, 0), (1, -1), "Helvetica"),
                ("FONTSIZE", (0, 0), (-1, -1), 10.5),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 0),
                ("RIGHTPADDING", (0, 0), (-1, -1), 4),
                ("TOPPADDING", (0, 0), (-1, -1), 4),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
            ]
        )
    )
    story.append(tabla)
    story.append(Spacer(1, 0.25 * cm))


def crear_pdf_rdi(registro: dict[str, Any]) -> BytesIO:
    buffer = BytesIO()

    doc = SimpleDocTemplate(
        buffer,
        pagesize=letter,
        rightMargin=1.5 * cm,
        leftMargin=1.5 * cm,
        topMargin=1.2 * cm,
        bottomMargin=1.3 * cm,
        title=f"RDI {registro.get('N RDI', '')}",
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

    story.append(Paragraph("Explicación de la RDI", styles["section_center"]))
    story.append(
        Paragraph(
            texto_seguro(registro.get("EXPLICACION DE LA RDI", "")),
            styles["normal"],
        )
    )

    agregar_imagenes(
        story,
        registro,
        ["IMAGEN 1 P", "IMAGEN 2 P"],
        "Imágenes Explicativas",
        styles,
    )

    agregar_archivos_adjuntos(
        story,
        registro,
        ["ARCHIVO 1 P", "ARCHIVO 2 P"],
        styles,
    )

    emitido_por = registro.get("EMITIDO POR", "")
    if emitido_por:
        story.append(Spacer(1, 0.4 * cm))
        story.append(
            Paragraph(
                f"Emitido por: {texto_seguro(emitido_por)}",
                styles["small"],
            )
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

    story.append(Paragraph("Respuesta de la RDI", styles["section_center"]))
    story.append(
        Paragraph(
            texto_seguro(registro.get("RESPUESTA DE LA RDI", "")),
            styles["normal"],
        )
    )

    agregar_imagenes(
        story,
        registro,
        ["IMAGEN 1 R", "IMAGEN 2 R", "IMAGEN 3 R", "IMAGEN 4 R"],
        "Imágenes Respuesta",
        styles,
    )

    agregar_archivos_adjuntos(
        story,
        registro,
        ["ARCHIVO 1 R", "ARCHIVO 2 R", "Video R."],
        styles,
    )

    respuesta_de = registro.get("RESPUESTA DE", "")
    if respuesta_de:
        story.append(Spacer(1, 0.45 * cm))
        story.append(
            Paragraph(
                f"Respuesta entregada por {texto_seguro(respuesta_de)}",
                styles["normal"],
            )
        )

    doc.build(story)
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
