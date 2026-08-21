"""
Descarga el sheet 'SF RESULTADOS CAMPOS CLAVES' via Sheets API (gcloud ADC),
calcula que campos faltan por oportunidad, y genera:
  - data/opportunities.json  (detalle por oportunidad, para la tabla)
  - data/aggregates.json     (rollups de hoy, para KPIs y graficos)
  - data/history/{fecha}.json + data/history_index.json (snapshots diarios para deltas semanales)
"""
import google.auth
import google.auth.transport.requests
import requests
import json
import os
import subprocess
from datetime import datetime, timezone, timedelta

SHEET_ID = "1gKitwBsX21Pvmfek81LDn_MJJhrYoo7dtdH9hqQg6V0"
TAB_NAME = "results-20260821-103100"
QUOTA_PROJECT = "meli-bi-data"

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
HISTORY_DIR = os.path.join(DATA_DIR, "history")

# Los 29 campos clave -- TODOS se tratan como obligatorios siempre, sin excepciones.
FIELDS = [
    "Hunter", "ACCOUNT_NAME", "MES_GANADO_COMISION_HISP", "cus_cust_id",
    "NOMBRE_DE_LA_OPORTUNIDAD", "SUPERVISOR", "SENIORITY", "ETAPA",
    "FECHA_DE_CREACION", "FECHA_DE_CIERRE", "PRODUCTO", "IMPORTE",
    "SIZE_SELLER", "SUBPRODUCTO", "ORIGEN", "PLATAFORMA", "ID_MARCA",
    "CANTIDAD_PDV_ENG", "Account_Tags__c", "ASESOR_MPAGO_OFF__C", "BRAND__C",
    "INDUSTRIA__C", "INDUSTRIA_Y_SUBSEGMENTO__C", "INDUSTRY", "ORIGEN__C",
    "PREMIUM_CX__C", "BILLINGSTATE", "Collector", "Cust_ID__c",
]

# Argentina (UTC-3), sin zoneinfo para no depender de tzdata en Windows
ARG_TZ = timezone(timedelta(hours=-3))


def fetch_rows():
    creds, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/spreadsheets.readonly"])
    creds.refresh(google.auth.transport.requests.Request())
    headers = {"Authorization": f"Bearer {creds.token}", "x-goog-user-project": QUOTA_PROJECT}

    meta_url = f"https://sheets.googleapis.com/v4/spreadsheets/{SHEET_ID}"
    r = requests.get(meta_url, headers=headers, params={"fields": "sheets.properties"})
    r.raise_for_status()
    sheet_props = [s["properties"] for s in r.json()["sheets"]]

    tab = TAB_NAME if any(p["title"] == TAB_NAME for p in sheet_props) else sheet_props[0]["title"]
    n_rows = next(p["gridProperties"]["rowCount"] for p in sheet_props if p["title"] == tab)

    rng = f"{tab}!A1:AE{n_rows}"
    url = f"https://sheets.googleapis.com/v4/spreadsheets/{SHEET_ID}/values/{requests.utils.quote(rng)}"
    r = requests.get(url, headers=headers, params={"valueRenderOption": "FORMATTED_VALUE"})
    r.raise_for_status()
    values = r.json().get("values", [])
    if not values:
        raise RuntimeError("El sheet no devolvio filas")

    header = values[0]
    col_idx = {name: header.index(name) for name in FIELDS if name in header}
    missing_cols = [name for name in FIELDS if name not in header]
    if missing_cols:
        print(f"AVISO: columnas no encontradas en el header del sheet: {missing_cols}")

    rows = values[1:]
    return col_idx, rows


def build_opportunities(col_idx, rows):
    opps = []
    for row in rows:
        def get(name):
            i = col_idx.get(name)
            if i is None or i >= len(row):
                return ""
            return str(row[i]).strip()

        missing_idx = [fi for fi, name in enumerate(FIELDS) if get(name) == ""]
        opps.append({
            "h": get("Hunter"),
            "s": get("SUPERVISOR"),
            "a": get("ACCOUNT_NAME"),
            "o": get("NOMBRE_DE_LA_OPORTUNIDAD"),
            "e": get("ETAPA"),
            "p": get("PRODUCTO"),
            "imp": get("IMPORTE"),
            "mc": len(missing_idx),
            "mf": missing_idx,
        })
    return opps


def build_aggregates(opps, as_of):
    def blank_group():
        return {"total": 0, "complete": 0, "missing_slots": 0, "field_missing": [0] * len(FIELDS)}

    by_sup = {}
    by_hunter = {}  # key: "SUPERVISOR||HUNTER"
    overall = blank_group()

    for o in opps:
        for bucket_map, key in ((by_sup, o["s"]), (by_hunter, f'{o["s"]}||{o["h"]}')):
            g = bucket_map.setdefault(key, blank_group())
            g["total"] += 1
            g["missing_slots"] += o["mc"]
            if o["mc"] == 0:
                g["complete"] += 1
            for fi in o["mf"]:
                g["field_missing"][fi] += 1

        overall["total"] += 1
        overall["missing_slots"] += o["mc"]
        if o["mc"] == 0:
            overall["complete"] += 1
        for fi in o["mf"]:
            overall["field_missing"][fi] += 1

    return {
        "as_of": as_of,
        "fields": FIELDS,
        "overall": overall,
        "by_supervisor": by_sup,
        "by_hunter": by_hunter,
    }


def main():
    os.makedirs(DATA_DIR, exist_ok=True)
    os.makedirs(HISTORY_DIR, exist_ok=True)

    col_idx, rows = fetch_rows()
    opps = build_opportunities(col_idx, rows)

    now = datetime.now(ARG_TZ)
    as_of = now.strftime("%Y-%m-%d")

    aggregates = build_aggregates(opps, as_of)

    with open(os.path.join(DATA_DIR, "opportunities.json"), "w", encoding="utf-8") as f:
        json.dump({"as_of": as_of, "fields": FIELDS, "opportunities": opps}, f, ensure_ascii=False)

    with open(os.path.join(DATA_DIR, "aggregates.json"), "w", encoding="utf-8") as f:
        json.dump(aggregates, f, ensure_ascii=False)

    # snapshot historico (uno por dia -- si corre 2 veces el mismo dia, se sobreescribe)
    hist_path = os.path.join(HISTORY_DIR, f"{as_of}.json")
    with open(hist_path, "w", encoding="utf-8") as f:
        json.dump(aggregates, f, ensure_ascii=False)

    index_path = os.path.join(DATA_DIR, "history_index.json")
    dates = sorted({fn[:-5] for fn in os.listdir(HISTORY_DIR) if fn.endswith(".json")})
    with open(index_path, "w", encoding="utf-8") as f:
        json.dump(dates, f)

    print(f"OK. {len(opps)} oportunidades procesadas. as_of={as_of}. Historial: {len(dates)} dias.")

    try:
        subprocess.run(["git", "-C", BASE_DIR, "add", "data"], check=True)
        result = subprocess.run(["git", "-C", BASE_DIR, "diff", "--cached", "--quiet"])
        if result.returncode == 0:
            print("Sin cambios en los datos, no se hace commit.")
        else:
            subprocess.run(["git", "-C", BASE_DIR, "commit", "-m", f"datos: {as_of}"], check=True)
            subprocess.run(["git", "-C", BASE_DIR, "push"], check=True)
            print("Publicado en GitHub Pages.")
    except subprocess.CalledProcessError as e:
        print(f"AVISO: no se pudo publicar en git ({e}). Los datos locales se generaron igual.")


if __name__ == "__main__":
    main()
