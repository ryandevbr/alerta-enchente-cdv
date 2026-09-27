# COLETOR DE DADOS DA UHE SÁ CARVALHO (CEMIG) — v2
# Alerta Enchente CDV
#
# Extrai em tempo real e grava em `historico_cemig`.


import os
import sys
import re
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from dotenv import load_dotenv
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
from supabase import create_client, Client


# CONFIG

load_dotenv()

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
if not SUPABASE_URL or not SUPABASE_KEY:
    print("Faltam SUPABASE_URL e/ou SUPABASE_KEY no .env")
    sys.exit(1)

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
log = logging.getLogger("cemig")

URL_CEMIG   = "https://www.cemig.com.br/usinas/uhe-sa-carvalho/"
TZ_BRASILIA = timezone(timedelta(hours=-3))

IDS = {
    "nivel_m":          "nivel_atual_valor",
    "volume_util_pct":  "volume_util_valor",
    "afluencia_m3s":    "afluencia_valor",
    "defluencia_m3s":   "defluencia_valor",
    "chuva_mm":         "chuva_valor",
    "nivel_maximorum_m": "nivel_maximorum_valor",
    "nivel_maximo_m":   "nivel_maximo_valor",
    "nivel_minimo_m":   "nivel_minimo_valor",
}

IDS_DATA = {
    "nivel_atual": "nivel_atual_data",
    "afluencia":   "afluencia_data",
    "defluencia":  "defluencia_data",
}

# PARSERS

def parse_numero_br(texto: str) -> Optional[float]:
    if not texto:
        return None
    match = re.search(r"(-?\d+(?:[.,]\d+)?)", texto)
    if not match:
        return None
    try:
        return float(match.group(1).replace(",", "."))
    except ValueError:
        return None


def parse_data_br(texto: str) -> Optional[str]:
    if not texto:
        return None
    match = re.search(
        r"(\d{2})/(\d{2})/(\d{4})\s*-\s*(\d{2}):(\d{2})",
        texto.strip()
    )
    if not match:
        return None
    dia, mes, ano, hora, minuto = map(int, match.groups())
    try:
        dt = datetime(ano, mes, dia, hora, minuto, tzinfo=TZ_BRASILIA)
        return dt.astimezone(timezone.utc).isoformat()
    except ValueError:
        return None

# SCRAPER

def coletar_dados_cemig() -> Optional[dict]:
    log.info(f"Acessando {URL_CEMIG}")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(
            user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/120.0.0.0 Safari/537.36"),
            locale="pt-BR",
        )
        page = ctx.new_page()

        try:
            page.goto(URL_CEMIG, wait_until="networkidle", timeout=90000)

            page.wait_for_function(
                """() => {
                    const el = document.getElementById('nivel_atual_valor');
                    return el && el.innerText && el.innerText.trim().length > 0;
                }""",
                timeout=60000,
            )
            page.wait_for_timeout(2000)

            dados = {}
            for chave, id_el in IDS.items():
                try:
                    texto = page.locator(f"#{id_el}").inner_text()
                    dados[chave] = parse_numero_br(texto)
                except Exception as e:
                    log.warning(f"   ⚠️ Não consegui ler '{chave}': {e}")
                    dados[chave] = None

            # Timestamps
            for chave, id_el in IDS_DATA.items():
                try:
                    texto = page.locator(f"#{id_el}").inner_text()
                    dados[f"ts_{chave}"] = parse_data_br(texto)
                except Exception:
                    dados[f"ts_{chave}"] = None

            # Usa o timestamp do nível como referência principal.
            #   Fallback para o momento da coleta se ausente.
            dados["data_hora"] = dados.get("ts_nivel_atual") or \
                                 datetime.now(timezone.utc).isoformat()

            dados["coletado_em"] = datetime.now(timezone.utc).isoformat()
            dados["usina"] = "UHE_SA_CARVALHO"
            dados["fonte"] = "cemig.com.br"

            log.info(f"Coleta: afluência={dados['afluencia_m3s']} "
                     f"defluência={dados['defluencia_m3s']} "
                     f"nível={dados['nivel_m']} "
                     f"vol_útil={dados['volume_util_pct']}%")
            return dados

        except PWTimeout as e:
            log.error(f"Timeout: {e}")
            return None
        except Exception as e:
            log.error(f"Erro: {e}")
            return None
        finally:
            browser.close()

# PERSISTÊNCIA

def gravar_no_banco(dados: dict) -> bool:
    """Faz upsert em `historico_cemig`."""
    # Remove chaves internas que não são colunas da tabela
    payload = {k: v for k, v in dados.items()
               if k not in ("ts_nivel_atual", "ts_afluencia", "ts_defluencia")}

    try:
        supabase.table("historico_cemig").upsert(
            payload,
            on_conflict="usina,data_hora",
        ).execute()
        log.info(f"Gravado em historico_cemig: {payload['data_hora']}")
        return True
    except Exception as e:
        log.error(f"Erro no upsert: {e}")
        return False

# MAIN

def main() -> int:
    log.info("=" * 60)
    log.info("COLETA CEMIG — UHE SÁ CARVALHO")
    log.info("=" * 60)

    dados = coletar_dados_cemig()
    if not dados:
        log.error("Falha na coleta.")
        return 1

    # Mostra no console
    print("\n" + "=" * 60)
    print("DADOS COLETADOS")
    print("=" * 60)
    for k, v in dados.items():
        print(f"  {k:22s}: {v}")
    print("=" * 60)

    # Grava no banco
    if not gravar_no_banco(dados):
        return 1

    log.info("=" * 60)
    log.info("Fim")
    log.info("=" * 60)
    return 0


if __name__ == "__main__":
    sys.exit(main())