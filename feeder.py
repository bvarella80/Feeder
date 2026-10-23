#!/usr/bin/env python3
"""
Feeder regulatório: monitora agências reguladoras (ANEEL, ARSESP, AGEPAR, ARSAE...),
o Diário Oficial e a imprensa, filtra por temas (revisão tarifária, encargos,
curtailment...) e envia só o que é novo por e-mail e/ou Telegram.

Uso:
  python feeder.py                 # execução normal
  python feeder.py --dry-run       # mostra o resultado, não envia nem salva estado
  python feeder.py --test-sources  # testa cada fonte e mostra quantos itens achou
"""
import argparse
import csv
import hashlib
import html
import json
import os
import re
import smtplib
import sys
import time
import unicodedata
from datetime import datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import quote_plus, urljoin, urlparse

import feedparser
import requests
import yaml
from bs4 import BeautifulSoup

BASE = Path(__file__).parent
STATE_FILE = BASE / "data" / "seen.json"
HISTORY_FILE = BASE / "data" / "historico.csv"
DIGEST_FILE = BASE / "data" / "ultimo_resumo.html"
FEED_FILE = BASE / "data" / "feed.json"
PAGE_FILE = BASE / "docs" / "index.html"
TEMPLATE_FILE = BASE / "templates" / "feed.html"
FEED_DAYS = 7
BRT = timezone(timedelta(hours=-3))
KEEP_DAYS = 180
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/126.0 Safari/537.36",
    "Accept-Language": "pt-BR,pt;q=0.9",
}

# Links de navegação comuns que não são conteúdo
NAV_NOISE = re.compile(
    r"(acessibilidade|mapa do site|fale conosco|ouvidoria|transpar[eê]ncia|"
    r"pol[ií]tica de privacidade|ir para o conte[uú]do|alto contraste|"
    r"perguntas frequentes|carta de servi[cç]os|login|entrar|facebook|"
    r"instagram|twitter|youtube|linkedin|whatsapp|flickr)",
    re.I,
)


# ----------------------------------------------------------------- utilidades
def norm(text: str) -> str:
    text = unicodedata.normalize("NFKD", text or "")
    text = "".join(c for c in text if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", text).lower().strip()


def item_id(url: str, title: str) -> str:
    key = (url or "").split("#")[0].rstrip("/") or title
    return hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]


def clean(text: str, limit: int = 400) -> str:
    text = BeautifulSoup(text or "", "html.parser").get_text(" ")
    text = re.sub(r"\s+", " ", html.unescape(text)).strip()
    return text[:limit] + ("…" if len(text) > limit else "")


def get(url: str, verify: bool = True) -> requests.Response:
    r = requests.get(url, headers=HEADERS, timeout=40, verify=verify)
    r.raise_for_status()
    return r


# ----------------------------------------------------------------- coletores
def collect_rss(src):
    resp = get(src["url"], src.get("verify_ssl", True))
    feed = feedparser.parse(resp.content)
    items = []
    for e in feed.entries:
        title = clean(e.get("title", ""), 300)
        link = e.get("link", "")
        snippet = clean(e.get("summary", ""))
        # Google Notícias repete o título no resumo; descarta nesse caso
        if norm(snippet).startswith(norm(title)[:40]):
            snippet = ""
        source_name = ""
        if "source" in e and hasattr(e.source, "title"):
            source_name = e.source.title
        items.append({"title": title, "url": link, "snippet": snippet,
                      "published": e.get("published", ""), "outlet": source_name})
    return items


def collect_html(src):
    resp = get(src["url"], src.get("verify_ssl", True))
    soup = BeautifulSoup(resp.content, "html.parser")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    scope = soup.select(src["selector"]) if src.get("selector") else [soup]
    base_host = urlparse(src["url"]).netloc.replace("www.", "")
    items, seen_urls = [], set()
    for area in scope:
        # ignora cabeçalho/rodapé/menus quando não há seletor
        if not src.get("selector"):
            for t in area.find_all(["nav", "footer", "header"]):
                t.decompose()
        for a in area.find_all("a", href=True):
            href = urljoin(src["url"], a["href"]).split("#")[0]
            title = clean(a.get("title") or a.get_text(" "), 300)
            if len(title) < 25 or NAV_NOISE.search(title):
                continue
            if not href.startswith("http"):
                continue
            host = urlparse(href).netloc.replace("www.", "")
            is_doc = re.search(r"\.(pdf|docx?|xlsx?|zip)$", href, re.I)
            if host != base_host and not is_doc:
                continue
            if href in seen_urls:
                continue
            seen_urls.add(href)
            items.append({"title": title, "url": href, "snippet": "",
                          "published": "", "outlet": ""})
    return items


def collect_dou(src):
    """Busca no portal do DOU (in.gov.br). Resultados vêm num JSON embutido na página."""
    period = src.get("period", "dia")  # dia | semana | mes
    url = ("https://www.in.gov.br/consulta/-/buscar/dou?q=" + quote_plus(src["query"]) +
           f"&s={src.get('section', 'do1')}&exactDate={period}&sortType=0&delta=100")
    resp = get(url)
    soup = BeautifulSoup(resp.content, "html.parser")
    data = None
    for script in soup.find_all("script", type="application/json"):
        try:
            parsed = json.loads(script.string or "")
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(parsed, dict) and "jsonArray" in parsed:
            data = parsed
            break
    if data is None:
        raise RuntimeError("estrutura do DOU não reconhecida (o portal pode ter mudado)")
    items = []
    for r in data.get("jsonArray", []):
        title = clean(r.get("title", ""), 300)
        art = r.get("artType", "")
        if art and norm(art) not in norm(title):
            title = f"{art} — {title}"
        items.append({
            "title": title,
            "url": "https://www.in.gov.br/web/dou/-/" + r.get("urlTitle", ""),
            "snippet": clean(r.get("content", "")),
            "published": r.get("pubDate", ""),
            "outlet": r.get("hierarchyStr", ""),
        })
    return items


def collect_doesp(src):
    """Diário Oficial do Estado de SP: API pública de busca (acervo desde 2023)."""
    api = "https://do-api-web-search.doe.sp.gov.br/v2/advanced-search/publications"
    today = datetime.now(BRT).date()
    start = today - timedelta(days=src.get("days", 2))
    items, seen_urls = [], set()
    for term in src["terms"]:
        params = {"FromDate": start.isoformat(), "ToDate": today.isoformat(),
                  "PageNumber": 1, "PageSize": 100, "SortField": "Date", "Terms[0]": term}
        r = requests.get(api, params=params, headers=HEADERS, timeout=40)
        r.raise_for_status()
        for it in r.json().get("items", []):
            slug = it.get("slug") or ""
            url = slug if slug.startswith("http") else "https://www.doe.sp.gov.br/" + slug.lstrip("/")
            if url in seen_urls:
                continue
            seen_urls.add(url)
            hier = it.get("hierarchy") or ""
            if isinstance(hier, list):
                hier = " / ".join(str(h) for h in hier)
            items.append({"title": clean(it.get("title", ""), 300), "url": url,
                          "snippet": clean(it.get("excerpt", "")),
                          "published": it.get("date", ""), "outlet": clean(str(hier), 120)})
        time.sleep(1)
    return items


COLLECTORS = {"rss": collect_rss, "html": collect_html, "dou": collect_dou,
              "doesp": collect_doesp}


# ----------------------------------------------------------------- classificação
def compile_themes(cfg):
    compiled = []
    for key, t in cfg["themes"].items():
        pats = [re.compile(r"(?<![a-z0-9])" + re.escape(norm(k)) + r"(?![a-z0-9])")
                for k in t["keywords"]]
        compiled.append((key, t["label"], t.get("priority", "media"), pats))
    excl = [re.compile(r"(?<![a-z0-9])" + re.escape(norm(k)) + r"(?![a-z0-9])")
            for k in cfg.get("exclude", [])]
    return compiled, excl


def classify(item, themes, excl):
    text = norm(f"{item['title']} {item['snippet']}")
    if any(p.search(text) for p in excl):
        return None
    hits, prio = [], "baixa"
    for _key, label, priority, pats in themes:
        if any(p.search(text) for p in pats):
            hits.append(label)
            if priority == "alta":
                prio = "alta"
            elif prio == "baixa":
                prio = "media"
    return hits, prio



# ----------------------------------------------------------------- notícias por setor
GNEWS_LOCALES = {
    "BR": ("pt-BR", "BR", "BR:pt-419"),
    "CL": ("es-419", "CL", "CL:es-419"),
    "AR": ("es-419", "AR", "AR:es-419"),
    "CO": ("es-419", "CO", "CO:es-419"),
    "MX": ("es-419", "MX", "MX:es-419"),
    "PE": ("es-419", "PE", "PE:es-419"),
    "US": ("en-US", "US", "US:en"),
}


def gnews_urls(terms, locale="BR", chunk=6):
    hl, gl, ceid = GNEWS_LOCALES.get(locale, GNEWS_LOCALES["BR"])
    for i in range(0, len(terms), chunk):
        q = " OR ".join(f'"{t}"' for t in terms[i:i + chunk])
        yield (f"https://news.google.com/rss/search?q={quote_plus('(' + q + ') when:1d')}"
               f"&hl={hl}&gl={gl}&ceid={ceid}")


def fold_keep_case(text):
    text = unicodedata.normalize("NFKD", text or "")
    return re.sub(r"\s+", " ", "".join(c for c in text if not unicodedata.combining(c)))


def term_pattern(term):
    """Inicial maiúscula = diferencia maiúsculas; minúscula = tanto faz."""
    t = fold_keep_case(term).strip()
    flags = 0 if t[:1].isupper() else re.I
    return re.compile(r"(?<![A-Za-z0-9])" + re.escape(t) + r"(?![A-Za-z0-9])", flags)


def strip_outlet(title, outlet):
    if outlet and title.endswith(" - " + outlet):
        return title[: -len(outlet) - 3].strip()
    return re.sub(r"\s+-\s+[^-]{2,40}$", "", title).strip()


def group_spec(grp):
    """Devolve (termos de busca, [(etiqueta, padrão)]) de um grupo."""
    search, pats = [], []
    for c in grp.get("companies", []):
        label = f"{c['name']} ({c['ticker']})" if c.get("ticker") else c["name"]
        names = [c["name"]] + list(c.get("aliases", []))
        search += names
        for n in names + ([c["ticker"]] if c.get("ticker") else []):
            pats.append((label, term_pattern(n)))
    extra = list(grp.get("terms", []))
    search += extra
    for m in grp.get("match") or extra:
        pats.append((m, term_pattern(m)))
    return search, pats


def run_news(cfg, state, now, errors):
    ncfg = cfg.get("news")
    if not ncfg:
        return []
    hi = [re.compile(r"(?<![a-z0-9])" + re.escape(norm(k)) + r"(?![a-z0-9])")
          for k in ncfg.get("highlight", [])]
    ex = [re.compile(r"(?<![a-z0-9])" + re.escape(norm(k)) + r"(?![a-z0-9])")
          for k in ncfg.get("exclude", [])]
    cap = ncfg.get("max_per_group", 25)
    out, fetched, added_ids = [], set(), set()
    for sector in ncfg["sectors"]:
        for grp in sector["groups"]:
            terms, pats = group_spec(grp)
            got = []
            for url in gnews_urls(terms, grp.get("locale", "BR")):
                try:
                    got += collect_rss({"url": url})
                except Exception as exc:
                    errors.append(f"Setores / {sector['name']} / {grp['name']}: "
                                  f"{type(exc).__name__}: {str(exc)[:120]}")
                time.sleep(1)  # gentileza com o Google Notícias
            added = 0
            for it in got:
                iid = item_id(it["url"], it["title"])
                if iid in state or iid in added_ids:
                    continue
                fetched.add(iid)
                title = strip_outlet(it["title"], it.get("outlet", ""))
                ntitle = norm(title)
                if any(p.search(ntitle) for p in ex):
                    continue
                ftitle = fold_keep_case(title)
                tags = list(dict.fromkeys(m for m, p in pats if p.search(ftitle)))
                if not tags or added >= cap:
                    continue
                pub = ""
                try:
                    pub = parsedate_to_datetime(it["published"]).astimezone(timezone.utc).isoformat()
                except Exception:
                    pass
                added += 1
                added_ids.add(iid)
                out.append({
                    "kind": "news", "id": iid, "title": title, "url": it["url"],
                    "snippet": "", "outlet": it.get("outlet", ""), "pub": pub,
                    "sector_name": sector["name"], "group": grp["name"],
                    "themes": tags, "agency": sector["name"], "sector": grp["name"],
                    "source": f"{sector['name']} / {grp['name']}",
                    "priority": "alta" if any(p.search(ntitle) for p in hi) else "baixa",
                })
    for iid in fetched:  # só marca como visto depois de todos os grupos olharem
        state[iid] = now.isoformat()
    # mesma notícia em grupos diferentes: fica no primeiro
    seen_titles, dedup = set(), []
    for it in out:
        k = norm(it["title"])[:80]
        if k not in seen_titles:
            seen_titles.add(k)
            dedup.append(it)
    return dedup


# ----------------------------------------------------------------- resumo IA
def ai_summaries(items, cfg):
    conf = cfg.get("ai_summary", {})
    key = os.getenv("ANTHROPIC_API_KEY")
    if not (conf.get("enabled") and key and items):
        return
    batch = items[: conf.get("max_items", 40)]
    payload = [{"id": it["id"], "agencia": it["agency"], "titulo": it["title"],
                "trecho": it["snippet"]} for it in batch]
    prompt = (
        "Você apoia um analista de equity que cobre utilities, bancos, non-banks, agro (açúcar, etanol e grãos), "
        "distribuição de combustíveis no Brasil e bancos da América Latina. "
        "Para cada item abaixo, escreva em português uma frase de até 30 palavras dizendo o que é "
        "e por que pode importar para resultado, tarifas, valuation ou risco regulatório das empresas. "
        "Se não houver informação suficiente, diga o que o título indica, sem inventar. "
        'Responda APENAS com JSON no formato [{"id": "...", "resumo": "..."}], sem markdown.\n\n'
        + json.dumps(payload, ensure_ascii=False)
    )
    try:
        r = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": key, "anthropic-version": "2023-06-01",
                     "content-type": "application/json"},
            json={"model": conf.get("model", "claude-haiku-5-5"), "max_tokens": 4000,
                  "messages": [{"role": "user", "content": prompt}]},
            timeout=120,
        )
        r.raise_for_status()
        text = "".join(b.get("text", "") for b in r.json()["content"])
        text = re.sub(r"^```(json)?|```$", "", text.strip(), flags=re.M).strip()
        by_id = {d["id"]: d["resumo"] for d in json.loads(text)}
        for it in batch:
            it["ai"] = by_id.get(it["id"], "")
    except Exception as exc:  # resumo é opcional; nunca derruba o envio
        print(f"[aviso] resumo por IA falhou: {exc}", file=sys.stderr)


# ----------------------------------------------------------------- saída
PRIO_ORDER = {"alta": 0, "media": 1, "baixa": 2}
PRIO_LABEL = {"alta": "🔴 Alta relevância", "media": "🟡 Relevância média", "baixa": "⚪ Outros itens novos"}


def render_html(items, errors, now, news=()):
    parts = [
        "<html><body style='font-family:Arial,Helvetica,sans-serif;max-width:760px;"
        "margin:auto;color:#1a1a1a'>",
        f"<h2 style='margin-bottom:4px'>Feeder</h2>"
        f"<div style='color:#666;margin-bottom:18px'>{now:%d/%m/%Y %H:%M} · "
        f"{len(items)} item(ns) novo(s)</div>",
    ]
    if os.getenv("FEED_URL"):
        parts.append(f"<p><a href='{html.escape(os.getenv('FEED_URL'))}' style='display:inline-block;"
                     "background:#1F5E8C;color:#fff;padding:8px 14px;border-radius:6px;"
                     "text-decoration:none'>Abrir o feed completo</a></p>")
    if not items:
        parts.append("<p>Nada novo nas agências reguladoras desde a última execução.</p>")
    for prio in ("alta", "media", "baixa"):
        group = [i for i in items if i["priority"] == prio]
        if not group:
            continue
        parts.append(f"<h3 style='border-bottom:1px solid #ddd;padding-bottom:4px'>"
                     f"{PRIO_LABEL[prio]} ({len(group)})</h3>")
        for it in group:
            tags = " ".join(
                f"<span style='background:#eef2f7;border-radius:4px;padding:1px 6px;"
                f"font-size:12px;margin-right:4px'>{html.escape(t)}</span>" for t in it["themes"])
            meta = " · ".join(x for x in [it["agency"], it["sector"], it.get("outlet", "")] if x)
            body = it.get("ai") or it["snippet"]
            parts.append(
                "<div style='margin:0 0 16px'>"
                f"<a href='{html.escape(it['url'])}' style='font-weight:bold;color:#0b4f9c;"
                f"text-decoration:none'>{html.escape(it['title'])}</a>"
                f"<div style='font-size:12px;color:#666;margin:2px 0'>{html.escape(meta)}</div>"
                + (f"<div style='font-size:14px'>{html.escape(body)}</div>" if body else "")
                + (f"<div style='margin-top:4px'>{tags}</div>" if tags else "")
                + "</div>")
    news_hi = [n for n in news if n["priority"] == "alta"]
    if news_hi:
        parts.append(f"<h3 style='border-bottom:1px solid #ddd;padding-bottom:4px;margin-top:28px'>"
                     f"Setores: destaques ({len(news_hi)} de {len(news)} notícias novas)</h3>")
        for sec in dict.fromkeys(n["sector_name"] for n in news_hi):
            parts.append(f"<div style='font-weight:bold;margin:14px 0 6px'>{html.escape(sec)}</div>")
            for n in [x for x in news_hi if x["sector_name"] == sec]:
                meta = " / ".join(x for x in [n["group"], n.get("outlet", "")] if x)
                parts.append(
                    f"<div style='margin:0 0 10px'><a href='{html.escape(n['url'])}' "
                    f"style='color:#0b4f9c;text-decoration:none'>{html.escape(n['title'])}</a>"
                    f"<div style='font-size:12px;color:#666'>{html.escape(meta)}</div>"
                    + (f"<div style='font-size:13px'>{html.escape(n['ai'])}</div>" if n.get("ai") else "")
                    + "</div>")
    if errors:
        parts.append("<hr><div style='font-size:12px;color:#a33'><b>Fontes com erro nesta "
                     "execução</b> (o site pode ter mudado ou estar fora do ar):<br>"
                     + "<br>".join(html.escape(e) for e in errors) + "</div>")
    parts.append("</body></html>")
    return "\n".join(parts)


def render_text(items, errors, now, news=()):
    lines = [f"📋 Feeder regulatório — {now:%d/%m %H:%M} — {len(items)} novo(s)"]
    if os.getenv("FEED_URL"):
        lines.append(f"Feed completo: {os.getenv('FEED_URL')}")
    for prio in ("alta", "media", "baixa"):
        group = [i for i in items if i["priority"] == prio]
        if group:
            lines.append(f"\n{PRIO_LABEL[prio]}")
            for it in group:
                lines.append(f"• [{it['agency']}] {it['title']}\n  {it['url']}")
    news_hi = [n for n in news if n["priority"] == "alta"]
    if news_hi:
        lines.append(f"\n📰 Setores: destaques ({len(news_hi)})")
        for n in news_hi:
            lines.append(f"• [{n['sector_name']}] {n['title']}\n  {n['url']}")
    if errors:
        lines.append(f"\n⚠️ {len(errors)} fonte(s) com erro")
    return "\n".join(lines)


def send_email(subject, html_body):
    host, to = os.getenv("SMTP_HOST"), os.getenv("EMAIL_TO")
    if not (host and to):
        return False
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = os.getenv("EMAIL_FROM") or os.getenv("SMTP_USER")
    msg["To"] = to
    msg.attach(MIMEText(html_body, "html", "utf-8"))
    with smtplib.SMTP(host, int(os.getenv("SMTP_PORT", "587")), timeout=60) as s:
        s.starttls()
        s.login(os.getenv("SMTP_USER"), os.getenv("SMTP_PASSWORD"))
        s.sendmail(msg["From"], [a.strip() for a in to.split(",")], msg.as_string())
    return True


def send_telegram(text):
    token, chat = os.getenv("TELEGRAM_BOT_TOKEN"), os.getenv("TELEGRAM_CHAT_ID")
    if not (token and chat):
        return False
    chunks, buf = [], ""
    for line in text.split("\n"):  # limite de 4096 caracteres por mensagem
        if len(buf) + len(line) + 1 > 3900:
            chunks.append(buf)
            buf = ""
        buf += line + "\n"
    chunks.append(buf)
    for c in chunks:
        requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                      json={"chat_id": chat, "text": c, "disable_web_page_preview": True},
                      timeout=30).raise_for_status()
    return True


# ----------------------------------------------------------------- estado
def load_state():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    return None


def save_state(state):
    cutoff = (datetime.now(BRT) - timedelta(days=KEEP_DAYS)).isoformat()
    state = {k: v for k, v in state.items() if v >= cutoff}
    STATE_FILE.parent.mkdir(exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=0, sort_keys=True), encoding="utf-8")


def append_history(items, now):
    new_file = not HISTORY_FILE.exists()
    with HISTORY_FILE.open("a", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f, delimiter=";")
        if new_file:
            w.writerow(["data", "prioridade", "agencia", "setor", "fonte", "temas",
                        "titulo", "url", "resumo"])
        for it in items:
            w.writerow([now.strftime("%Y-%m-%d %H:%M"), it["priority"], it["agency"],
                        it["sector"], it["source"], " | ".join(it["themes"]), it["title"],
                        it["url"], it.get("ai") or it["snippet"]])


# ----------------------------------------------------------------- página consolidada
def compact(it, now, run_id):
    d = {"id": it["id"], "k": it.get("kind", "reg"), "t": it["title"], "u": it["url"],
         "s": it.get("ai") or it["snippet"], "ag": it["agency"], "se": it["sector"],
         "th": it["themes"], "p": it["priority"], "src": it["source"],
         "out": it.get("outlet", ""), "ts": now.isoformat(), "run": run_id}
    if it.get("pub"):
        d["pub"] = it["pub"]
    return d


def update_feed(new_items, now, run_id):
    feed = json.loads(FEED_FILE.read_text(encoding="utf-8")) if FEED_FILE.exists() else []
    for it in new_items:
        feed.append(compact(it, now, run_id))
    cutoff = (now - timedelta(days=FEED_DAYS)).isoformat()
    feed = [f for f in feed if f["ts"] >= cutoff]
    FEED_FILE.parent.mkdir(exist_ok=True)
    FEED_FILE.write_text(json.dumps(feed, ensure_ascii=False), encoding="utf-8")
    return feed


def render_page(feed, meta, out=PAGE_FILE):
    tpl = TEMPLATE_FILE.read_text(encoding="utf-8")
    js = lambda o: json.dumps(o, ensure_ascii=False).replace("</", "<\\/")
    page = (tpl.replace("/*__FEED_DATA__*/[]", js(feed))
               .replace("/*__FEED_META__*/{}", js(meta)))
    out.parent.mkdir(exist_ok=True)
    out.write_text(page, encoding="utf-8")


# ----------------------------------------------------------------- principal
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--test-sources", action="store_true")
    ap.add_argument("--config", default=str(BASE / "config.yaml"))
    args = ap.parse_args()

    cfg = yaml.safe_load(Path(args.config).read_text(encoding="utf-8"))
    themes, excl = compile_themes(cfg)
    now = datetime.now(BRT)

    state = load_state()
    first_run = state is None
    state = state or {}

    new_items, errors = [], []
    for src in cfg["sources"]:
        try:
            raw = COLLECTORS[src["type"]](src)
        except Exception as exc:
            errors.append(f"{src['name']}: {type(exc).__name__}: {str(exc)[:150]}")
            if args.test_sources:
                print(f"❌ {src['name']}: {exc}")
            continue
        if args.test_sources:
            print(f"✅ {src['name']}: {len(raw)} itens. Ex.: "
                  + (raw[0]['title'][:90] if raw else "(nenhum)"))
            continue
        for it in raw:
            iid = item_id(it["url"], it["title"])
            if iid in state:
                continue
            state[iid] = now.isoformat()
            result = classify(it, themes, excl)
            if result is None:
                continue
            hits, prio = result
            if src.get("require_match", True) and not hits:
                continue
            it.update(id=iid, source=src["name"], agency=src["agency"],
                      sector=src.get("sector", ""), themes=hits, priority=prio)
            new_items.append(it)

    if args.test_sources:
        return

    # Mesma notícia vinda de fontes diferentes: mantém uma (por título normalizado)
    dedup, titles = [], set()
    for it in sorted(new_items, key=lambda i: PRIO_ORDER[i["priority"]]):
        k = norm(it["title"])[:80]
        if k not in titles:
            titles.add(k)
            dedup.append(it)
    new_items = sorted(dedup, key=lambda i: (PRIO_ORDER[i["priority"]], i["agency"]))

    news_items = run_news(cfg, state, now, errors)

    run_id = now.strftime("%Y%m%d%H%M")
    meta = {"updated": now.isoformat(), "run_id": run_id, "errors": errors,
            "sources_total": len(cfg["sources"]),
            "sectors": [{"name": s["name"], "groups": [g["name"] for g in s["groups"]]}
                        for s in cfg.get("news", {}).get("sectors", [])]}

    if first_run and not args.dry_run:
        # Primeira execução: só memoriza o que já existe, para não lotar sua caixa
        save_state(state)
        render_page(update_feed(news_items, now, run_id), meta)
        msg = (f"Feeder ativado. {len(state)} itens existentes foram memorizados; a partir da "
               f"próxima execução você recebe só o que for novo. A aba Setores já começa com "
               f"{len(news_items)} notícias das últimas 24h.")
        if errors:
            msg += "\n\nFontes com erro:\n" + "\n".join(errors)
        print(msg)
        send_email("Feeder regulatório ativado ✅", f"<pre>{html.escape(msg)}</pre>")
        send_telegram(msg)
        return

    news_hi = [i for i in news_items if i["priority"] == "alta"]
    ai_summaries(new_items + news_hi, cfg)
    html_body = render_html(new_items, errors, now, news_items)
    text_body = render_text(new_items, errors, now, news_items)
    DIGEST_FILE.parent.mkdir(exist_ok=True)
    DIGEST_FILE.write_text(html_body, encoding="utf-8")

    if args.dry_run:
        existing = json.loads(FEED_FILE.read_text(encoding="utf-8")) if FEED_FILE.exists() else []
        preview = existing + [compact(i, now, run_id) for i in new_items + news_items]
        render_page(preview, meta, BASE / "data" / "preview.html")
        print("Prévia da página: data/preview.html\n")
        print(text_body)
        print(f"\nSetores: {len(news_items)} notícias novas ({len(news_hi)} destaques)")
        if errors:
            print("\nErros:\n" + "\n".join(errors))
        return

    n_high = sum(1 for i in new_items if i["priority"] == "alta")
    if new_items or news_hi or cfg.get("send_when_empty"):
        subject = (f"[Feeder] Regulatório: {len(new_items)} novo(s)"
                   + (f" ({n_high} alta)" if n_high else "")
                   + f" · Setores: {len(news_hi)} destaque(s) · {now:%d/%m %Hh}")
        sent = send_email(subject, html_body)
        sent = send_telegram(text_body) or sent
        if not sent:
            print("[aviso] nenhum canal de envio configurado (e-mail ou Telegram)")
    if new_items or news_items:
        append_history(new_items + news_items, now)
    render_page(update_feed(new_items + news_items, now, run_id), meta)
    save_state(state)
    print(text_body)


if __name__ == "__main__":
    main()
