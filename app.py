"""
App local: sube PDF de órdenes Shopify + planilla (Excel/CSV) con SKUs en stock;
descarga Excel con ítems del PDF que no están en la planilla.
"""
from __future__ import annotations

import io
import os
import re
import tempfile

import pandas as pd
from flask import Flask, render_template_string, request, send_file

from shopify_pdf_parser import LineItem, parse_orders_from_pdf

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 50 * 1024 * 1024  # 50 MB

HTML = """<!DOCTYPE html>
<html lang="es">
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1"/>
  <title>Órdenes vs stock</title>
  <style>
    :root { font-family: system-ui, sans-serif; }
    body { max-width: 42rem; margin: 2rem auto; padding: 0 1rem; line-height: 1.5; }
    h1 { font-size: 1.35rem; }
    label { display: block; margin-top: 1rem; font-weight: 600; }
    input[type=file] { margin-top: 0.35rem; }
    button { margin-top: 1.25rem; padding: 0.55rem 1.1rem; font-size: 1rem; cursor: pointer; }
    .hint { color: #444; font-size: 0.9rem; margin-top: 0.25rem; }
    .err { color: #a00; margin-top: 1rem; }
    .ok { color: #060; margin-top: 1rem; }
    code { background: #f0f0f0; padding: 0.1rem 0.35rem; border-radius: 4px; }
  </style>
</head>
<body>
  <h1>Comparar PDF de órdenes (Shopify) con planilla de stock</h1>
  <p>Subí el PDF de picking y la planilla base. La comparación se hace por
     <strong>Item(SKU) + Color + Talle</strong> y se considera con stock solo si
     <strong>Ubicación</strong> tiene contenido.</p>
  <form method="post" enctype="multipart/form-data">
    <label for="pdf">PDF de órdenes</label>
    <input type="file" name="pdf" id="pdf" accept=".pdf,application/pdf" required/>
    <label for="sheet">Planilla (Excel .xlsx / .xls o CSV)</label>
    <input type="file" name="sheet" id="sheet" accept=".csv,.xlsx,.xls,text/csv" required/>
    <p class="hint">Columnas esperadas de tu base: <code>Item</code>, <code>Color</code>, <code>Talle</code>, <code>Ubicación</code>.
      Si los nombres cambian levemente, se detectan automáticamente.</p>
    <button type="submit">Generar Excel</button>
  </form>
  {% if error %}<p class="err">{{ error }}</p>{% endif %}
  {% if message %}<p class="ok">{{ message }}</p>{% endif %}
  <hr style="margin-top:2rem"/>
  <p class="hint">Para usarlo en tu PC: en esta carpeta ejecutá <code>python app.py</code> y abrí
    <code>http://127.0.0.1:5055</code> en el navegador.</p>
</body>
</html>
"""


def _normalize_sku(v: object) -> str | None:
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return None
    s = str(v).strip()
    if not s or s.lower() == "nan":
        return None
    if s.endswith(".0") and s[:-2].isdigit():
        s = s[:-2]
    return s


def _normalize_text(v: object) -> str:
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return ""
    return str(v).strip().upper()


def _pick_col(df: pd.DataFrame, preferred: list[str], aliases_regex: str) -> str:
    normalized = {str(c).strip().casefold(): c for c in df.columns}
    for p in preferred:
        if p.casefold() in normalized:
            return normalized[p.casefold()]
    rx = re.compile(aliases_regex, re.I)
    for c in df.columns:
        if rx.match(str(c).strip()):
            return c
    raise ValueError(f"No encontré columna compatible con: {aliases_regex}")


def _load_stock_keys(file_storage) -> set[tuple[str, str, str]]:
    name = (file_storage.filename or "").lower()
    raw = file_storage.read()
    if name.endswith(".csv"):
        df = pd.read_csv(io.BytesIO(raw), dtype=str, encoding_errors="replace")
    elif name.endswith(".xlsx") or name.endswith(".xls"):
        df = pd.read_excel(io.BytesIO(raw), dtype=str)
    else:
        try:
            df = pd.read_csv(io.BytesIO(raw), dtype=str, encoding_errors="replace")
        except Exception:
            df = pd.read_excel(io.BytesIO(raw), dtype=str)

    col_item = _pick_col(df, ["Item"], r"^(item|sku|cod_item|id_item)$")
    col_color = _pick_col(df, ["Color"], r"^(color)$")
    col_talle = _pick_col(df, ["Talle"], r"^(talle|talla|size)$")
    col_ubic = _pick_col(df, ["Ubicación", "Ubicacion"], r"^(ubicacion|ubicación|location)$")

    keys: set[tuple[str, str, str]] = set()
    for _, row in df.iterrows():
        ubic = _normalize_text(row.get(col_ubic))
        if not ubic:
            continue
        item = _normalize_sku(row.get(col_item))
        color = _normalize_text(row.get(col_color))
        talle = _normalize_text(row.get(col_talle))
        if not item or not color or not talle:
            continue
        keys.add((item, color, talle))
        if item.isdigit():
            keys.add((str(int(item)), color, talle))
    return keys


def _missing_rows(items: list[LineItem], in_stock_keys: set[tuple[str, str, str]]) -> list[dict]:
    rows: list[dict] = []

    def has_stock(sku: str, color: str, talle: str) -> bool:
        key = (_normalize_sku(sku) or "", _normalize_text(color), _normalize_text(talle))
        if key in in_stock_keys:
            return True
        if key[0].isdigit():
            try:
                alt = (str(int(key[0])), key[1], key[2])
                if alt in in_stock_keys:
                    return True
            except ValueError:
                pass
        return False

    for it in items:
        if not has_stock(it.sku, it.color, it.size):
            rows.append(
                {
                    "numero_orden": it.order_id,
                    "sku": it.sku,
                    "cantidad": it.qty,
                    "color": it.color,
                    "talle": it.size,
                    "descripcion": it.description,
                }
            )
    return rows


@app.route("/", methods=["GET", "POST"])
def index():
    error = None
    message = None
    if request.method == "POST":
        pf = request.files.get("pdf")
        sf = request.files.get("sheet")
        if not pf or not pf.filename:
            error = "Falta el PDF."
        elif not sf or not sf.filename:
            error = "Falta la planilla."
        else:
            tmp_path = None
            try:
                fd, tmp_path = tempfile.mkstemp(suffix=".pdf")
                os.close(fd)
                pf.save(tmp_path)
                items = parse_orders_from_pdf(tmp_path)
                in_stock_keys = _load_stock_keys(sf)
                rows = _missing_rows(items, in_stock_keys)
                if not rows:
                    message = "No hay artículos fuera del listado (o no se pudieron leer ítems del PDF)."
                    return render_template_string(HTML, error=None, message=message)
                out_df = pd.DataFrame(rows)
                buf = io.BytesIO()
                with pd.ExcelWriter(buf, engine="openpyxl") as w:
                    out_df.to_excel(w, index=False, sheet_name="sin_stock")
                buf.seek(0)
                return send_file(
                    buf,
                    as_attachment=True,
                    download_name="ordenes_articulos_sin_stock.xlsx",
                    mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                )
            except Exception as e:
                error = str(e)
            finally:
                if tmp_path and os.path.isfile(tmp_path):
                    try:
                        os.unlink(tmp_path)
                    except OSError:
                        pass
    return render_template_string(HTML, error=error, message=message)


if __name__ == "__main__":
    import os

    host = os.environ.get("FLASK_RUN_HOST", "127.0.0.1")
    port = int(os.environ.get("PORT", "5055"))
    app.run(host=host, port=port, debug=os.environ.get("FLASK_DEBUG") == "1")
