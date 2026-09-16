"""
Genera el mail semanal de ranking de campos clave: cuentas corregidas en la
ultima semana + top 10 hunters que mas corrigieron + top 10 que menos.

Compara data/opportunities.json (HEAD) contra la version de ~7 dias atras via
git history. IMPORTANTE: la lista de FIELDS puede haber cambiado en el medio,
asi que el snapshot viejo se re-evalua usando la lista de campos VIGENTE HOY
(ver [[project_campos_clave_dashboard]] en la memoria -- si no se hace esto,
cuentas que "se resuelven" solo porque se saco un campo de la lista aparecen
como falsos positivos).

Salida (carpeta mail_output/):
  - subject.txt
  - body.html   (referencia la imagen como cid:avance_chart)
  - chart.png
No manda el mail -- eso lo hace el llamador (rutina en la nube) via Gmail MCP.
"""
import json
import os
import subprocess
from datetime import datetime, timezone, timedelta

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
OUT_DIR = os.path.join(BASE_DIR, "mail_output")
DASH_URL = "https://tnarbebury.github.io/campos-clave-dashboard/"

# Hunters excluidos de este mail a pedido explicito del usuario (2026-09-16).
EXCLUDE_HUNTERS = {"Maria Martha Vautier", "Agustin Mac"}

ARG_TZ = timezone(timedelta(hours=-3))


def ensure_full_history():
    is_shallow = subprocess.run(
        ["git", "-C", BASE_DIR, "rev-parse", "--is-shallow-repository"],
        capture_output=True, text=True, encoding="utf-8", errors="replace"
    ).stdout.strip()
    if is_shallow == "true":
        subprocess.run(["git", "-C", BASE_DIR, "fetch", "--unshallow"], check=True)


def get_commit_dates():
    """Un commit por fecha (el mas reciente de ese dia) para data/opportunities.json."""
    log = subprocess.run(
        ["git", "-C", BASE_DIR, "log", "--format=%H|%ad", "--date=short", "--", "data/opportunities.json"],
        capture_output=True, text=True, encoding="utf-8", errors="replace", check=True
    ).stdout.strip().splitlines()
    by_date = {}
    for line in log:
        sha, date = line.split("|")
        by_date.setdefault(date, sha)  # el primero que aparece es el mas reciente (git log es desc)
    return by_date  # {"2026-09-16": sha, ...}


def load_snapshot_from_git(sha):
    raw = subprocess.run(
        ["git", "-C", BASE_DIR, "show", f"{sha}:data/opportunities.json"],
        capture_output=True, text=True, encoding="utf-8", errors="replace", check=True
    ).stdout
    return json.loads(raw)


def account_status(opps, fields, current_fields_set):
    """(hunter, account) -> True si tiene AL MENOS UNA oportunidad con campos
    faltantes, evaluado SOLO contra current_fields_set (normaliza campos que
    ya no se evaluan)."""
    status = {}
    for o in opps:
        mf_names = [fields[i] for i in o["mf"] if fields[i] in current_fields_set]
        key = (o["h"], o["a"])
        status[key] = status.get(key, False) or (len(mf_names) > 0)
    return status


def compute_weekly_stats():
    ensure_full_history()

    with open(os.path.join(BASE_DIR, "data", "opportunities.json"), encoding="utf-8") as f:
        new = json.load(f)
    new_fields = new["fields"]
    current_fields_set = set(new_fields)

    by_date = get_commit_dates()
    dates_sorted = sorted(by_date.keys())
    today = new["as_of"]
    today_ms = datetime.strptime(today, "%Y-%m-%d").timestamp()

    target_date = dates_sorted[0]
    for d in dates_sorted:
        if today_ms - datetime.strptime(d, "%Y-%m-%d").timestamp() >= 6.5 * 24 * 3600:
            target_date = d
    if target_date == today:
        raise RuntimeError("No hay suficiente historial (menos de ~7 dias) para comparar")

    old = load_snapshot_from_git(by_date[target_date])
    old_fields = old["fields"]

    old_status = account_status(old["opportunities"], old_fields, current_fields_set)
    new_status = account_status(new["opportunities"], new_fields, current_fields_set)

    old_incomplete = {k for k, v in old_status.items() if v and k[0] not in EXCLUDE_HUNTERS}

    resolved = []
    still_pending = 0
    for k in old_incomplete:
        if k not in new_status:
            continue
        if new_status[k] is False:
            resolved.append(k)
        else:
            still_pending += 1

    by_hunter_resolved = {}
    by_hunter_pending = {}
    for k in old_incomplete:
        by_hunter_pending[k[0]] = by_hunter_pending.get(k[0], 0) + 1
    for h, a in resolved:
        by_hunter_resolved[h] = by_hunter_resolved.get(h, 0) + 1

    ranking = [(h, by_hunter_resolved.get(h, 0), pending) for h, pending in by_hunter_pending.items()]
    top10 = sorted(ranking, key=lambda x: -x[1])[:10]
    bottom10 = sorted(ranking, key=lambda x: x[1])[:10]

    return {
        "old_date": target_date,
        "new_date": today,
        "total_pending_old": len(old_incomplete),
        "total_resolved": len(resolved),
        "still_pending": still_pending,
        "top10": top10,
        "bottom10": bottom10,
    }


def render_chart(stats, out_path):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    before = stats["total_pending_old"]
    after = stats["still_pending"]
    resolved = stats["total_resolved"]

    fig, ax = plt.subplots(figsize=(4.2, 2.6), dpi=100)
    bars = ax.bar(["Hace 1 semana", "Hoy"], [before, after], color=["#898781", "#2a78d6"], width=0.5)
    for b, v in zip(bars, [before, after]):
        ax.text(b.get_x() + b.get_width() / 2, v + max(before, 1) * 0.02, str(v),
                 ha="center", fontsize=11, fontweight="bold", color="#0b0b0b")
    ax.set_ylim(0, before * 1.15 if before else 1)
    ax.set_ylabel("Cuentas pendientes", fontsize=8, color="#52514e")
    ax.set_title(f"Bajaron de {before} a {after}  (-{resolved})", fontsize=10, color="#0b0b0b", pad=10)
    for spine in ["top", "right"]:
        ax.spines[spine].set_visible(False)
    ax.tick_params(axis="x", labelsize=9, colors="#0b0b0b")
    ax.tick_params(axis="y", labelsize=7, colors="#898781")
    ax.spines["left"].set_color("#e1e0d9")
    ax.spines["bottom"].set_color("#e1e0d9")
    ax.grid(axis="y", color="#e1e0d9", linewidth=0.6)
    ax.set_axisbelow(True)
    plt.tight_layout()
    plt.savefig(out_path, facecolor="white")
    plt.close(fig)


def render_table_rows(rows, color):
    out = []
    for h, r, p in rows:
        out.append(
            f'<tr style="border-bottom:1px solid #ddd"><td>{h}</td>'
            f'<td align="center" style="color:{color};font-weight:bold">{r}</td>'
            f'<td align="center">{p}</td></tr>'
        )
    return "\n".join(out)


def render_html(stats):
    top_rows = render_table_rows(stats["top10"], "#0ca30c")
    bottom_rows = render_table_rows(stats["bottom10"], "#d03b3b")
    return f'''<div style="font-family:Arial,Helvetica,sans-serif;font-size:14px;color:#222222;line-height:1.5;max-width:640px">

<p>Hola,</p>

<p>Este es el resumen semanal del tablero de <b>Campos Clave</b> de Salesforce, comparando el estado de hoy ({stats['new_date']}) contra el de hace una semana ({stats['old_date']}).</p>

<p><b>{stats['total_resolved']} cuentas se completaron</b> esta semana, de las {stats['total_pending_old']} que estaban pendientes el {stats['old_date']}. Quedan {stats['still_pending']} cuentas todavia incompletas.</p>

<p><img src="cid:avance_chart" alt="Cuentas pendientes: bajaron de {stats['total_pending_old']} a {stats['still_pending']}" width="420"></p>

<p><b>Top 10 &mdash; hunters que mas cuentas corrigieron</b></p>
<table cellpadding="4" cellspacing="0" style="border-collapse:collapse;font-size:13px">
<tr style="border-bottom:1px solid #999"><th align="left">Hunter</th><th align="center">Corregidas</th><th align="center">Pendientes ({stats['old_date']})</th></tr>
{top_rows}
</table>

<p><b>Top 10 &mdash; hunters que menos cuentas corrigieron</b></p>
<table cellpadding="4" cellspacing="0" style="border-collapse:collapse;font-size:13px">
<tr style="border-bottom:1px solid #999"><th align="left">Hunter</th><th align="center">Corregidas</th><th align="center">Pendientes ({stats['old_date']})</th></tr>
{bottom_rows}
</table>
<p style="font-size:11px;color:#666">*Solo incluye hunters que tenian al menos 1 cuenta pendiente hace una semana.</p>

<p>Tablero actualizado: <a href="{DASH_URL}">{DASH_URL}</a></p>

</div>'''


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    stats = compute_weekly_stats()
    render_chart(stats, os.path.join(OUT_DIR, "chart.png"))
    html = render_html(stats)

    with open(os.path.join(OUT_DIR, "body.html"), "w", encoding="utf-8") as f:
        f.write(html)
    with open(os.path.join(OUT_DIR, "subject.txt"), "w", encoding="utf-8") as f:
        f.write(f"Reporte semanal Campos Clave SF — {stats['new_date']}")

    print(json.dumps(stats, indent=2, ensure_ascii=False))
    print(f"\nOK. Archivos generados en {OUT_DIR}")


if __name__ == "__main__":
    main()
