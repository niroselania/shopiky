"""
Extrae líneas de pedido desde el PDF de picking de Shopify (Patagonia / layout similar).
Cada ítem: cantidad, SKU numérico, nombre - COLOR / TALLE.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterator

import pdfplumber

# Líneas de ítem: cantidad + SKU (solo dígitos) + texto + " / " + talle al final
_ITEM_RE = re.compile(
    r"(\d+)\s+(\d+)\s+(.+?)\s/\s(\S+)",
    re.DOTALL,
)

# Números de orden tipo #93969 o ANDREANI #93970
_ORDER_HASH_RE = re.compile(r"#(\d{4,})")

# Líneas que no son ítems (ruido típico del PDF)
_NOISE_PREFIXES = (
    "http://",
    "https://",
    "CANT. SKU",
    "PICK UP",
    "ENVIO N°",
    "TREGGO N°",
    "N° ORDEN",
    "Precio total",
    "Total pagado",
    "Monto pendiente",
    "Número de seguimiento",
    "Escanear para",
    "Etiqueta de Envío",
    "BOLSA REGALO",
    "No se han realizado",
    "PENDIENTE DE PAGO",
    "MIRAR CANTIDADES",
)


@dataclass
class LineItem:
    order_id: str
    qty: int
    sku: str
    description: str
    color: str
    size: str


def _split_color_desc(rest: str) -> tuple[str, str]:
    """rest = 'Nombre producto - COLOR' (último ' - ' separa color)."""
    rest = rest.strip()
    if " - " in rest:
        name, color = rest.rsplit(" - ", 1)
        return name.strip(), color.strip()
    return rest, ""


def _is_noise_line(line: str) -> bool:
    s = line.strip()
    if not s:
        return True
    low = s.lower()
    for p in _NOISE_PREFIXES:
        if s.startswith(p) or low.startswith(p.lower()):
            return True
    if s.startswith("📬") or s.startswith("🔗") or s.startswith("⚠"):
        return True
    return False


def extract_pdf_text(path: str) -> str:
    parts: list[str] = []
    with pdfplumber.open(path) as pdf:
        for page in pdf.pages:
            t = page.extract_text() or ""
            parts.append(t)
    return "\n".join(parts)


def parse_orders_from_text(text: str) -> list[LineItem]:
    """
    Asocia ítems a órdenes según la cantidad de #órdenes en la cabecera inmediata anterior
    (1 o 2 columnas por página en el PDF de ejemplo).
    """
    lines = text.splitlines()
    active_orders: list[str] = []
    out: list[LineItem] = []

    for line in lines:
        raw = line.rstrip()
        if _is_noise_line(raw):
            continue

        hashes = _ORDER_HASH_RE.findall(raw)
        if hashes:
            active_orders = list(hashes)
            continue

        if not active_orders:
            continue

        matches = list(_ITEM_RE.finditer(raw))
        if not matches:
            continue

        n_orders = len(active_orders)
        for i, m in enumerate(matches):
            qty_s, sku_s, rest, size = m.groups()
            qty = int(qty_s)
            sku = sku_s.strip()
            desc, color = _split_color_desc(rest)
            if n_orders == 1:
                oid = active_orders[0]
            else:
                oid = active_orders[0] if i == 0 else active_orders[1]
            out.append(
                LineItem(
                    order_id=oid,
                    qty=qty,
                    sku=sku,
                    description=desc,
                    color=color,
                    size=size.strip(),
                )
            )
    return out


def _parse_column_text(column_text: str) -> list[LineItem]:
    """Mitad de página: a lo sumo un #orden por bloque; ítems solo de esa columna."""
    return parse_orders_from_text(column_text)


def parse_orders_from_pdf(path: str) -> list[LineItem]:
    """
    Usa recorte izquierda/derecha de cada página para no mezclar ítems de dos órdenes
    cuando el texto plano los concatena en una sola línea.
    """
    out: list[LineItem] = []
    with pdfplumber.open(path) as pdf:
        for page in pdf.pages:
            w = float(page.width)
            h = float(page.height)
            mid = w * 0.52
            try:
                left = page.within_bbox((0, 0, mid, h))
                right = page.within_bbox((mid, 0, w, h))
                lt = left.extract_text() or ""
                rt = right.extract_text() or ""
            except Exception:
                lt = page.extract_text() or ""
                rt = ""
            out.extend(_parse_column_text(lt))
            if rt.strip():
                out.extend(_parse_column_text(rt))
            elif not lt.strip():
                out.extend(parse_orders_from_text(page.extract_text() or ""))
    return out


def iter_unique_skus(items: list[LineItem]) -> Iterator[str]:
    seen: set[str] = set()
    for it in items:
        if it.sku not in seen:
            seen.add(it.sku)
            yield it.sku
