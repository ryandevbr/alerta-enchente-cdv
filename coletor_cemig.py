# COLETOR DE DADOS DA UHE SÁ CARVALHO (CEMIG) — v2
# Alerta Enchente CDV
#
# Extrai em tempo real e grava em `historico_cemig`.

import re
import sys
from datetime import datetime, timezone
from typing import Any, Optional

from playwright.sync_api import TimeoutError as PWTimeout, sync_playwright

from core.config import TZ_BRASILIA, configurar_logger
from core.db import obter_supabase

log = configurar_logger("cemig", formato_data="%H:%M:%S")

URL_CEMIG = "https://www.cemig.com.br/usinas/uhe-sa-carvalho/"

IDS_VALORES = {
    "nivel_m": "nivel_atual_valor",
    "volume_util_pct": "volume_util_valor",
    "afluencia_m3s": "afluencia_valor",
    "defluencia_m3s": "defluencia_valor",
    "chuva_mm": "chuva_valor",
    "nivel_maximorum_m": "nivel_maximorum_valor",
    "nivel_maximo_m": "nivel_maximo_valor",
    "nivel_minimo_m": "nivel_minimo_valor",
}

IDS_DATAS = {
    "nivel_atual": "nivel_atual_data",
    "afluencia": "afluencia_data",
    "defluencia": "defluencia_data",
}

_RE_NUMERO_BR = re.compile(r"(-?\d+(?:[.,]\d+)?)")
_RE_DATA_BR = re.compile(r"(\d{2})/(\d{2})/(\d{4})\s*-\s*(\d{2}):(\d{2})")


def parse_numero_br(texto: Optional[str]) -> Optional[float]:
    """Extrai o primeiro número decimal (padrão brasileiro ou ponto) do texto."""
    if not texto:
        return None
    match = _RE_NUMERO_BR.search(texto)
    if not match:
        return None
    try:
        return float(match.group(1).replace(",", "."))
    except ValueError:
        return None


def parse_data_br(texto: Optional[str]) -> Optional[str]:
    """Converte data 'dd/mm/yyyy - HH:MM' (BRT) para ISO-8601 UTC."""
    if not texto:
        return None
    match = _RE_DATA_BR.search(texto.strip())
    if not match:
        return None
    dia, mes, ano, hora, minuto = map(int, match.groups())
    try:
        dt = datetime(ano, mes, dia, hora, minuto, tzinfo=TZ_BRASILIA)
        return dt.astimezone(timezone.utc).isoformat()
    except ValueError:
        return None


def coletar_dados_cemig() -> Optional[dict[str, Any]]:
    """Realiza scraping da página da UHE Sá Carvalho via Playwright."""
    log.info("Acessando %s", URL_CEMIG)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
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

            dados: dict[str, Any] = {}
            for chave, id_el in IDS_VALORES.items():
                try:
                    texto = page.locator(f"#{id_el}").inner_text()
                    dados[chave] = parse_numero_br(texto)
                except Exception as exc:
                    log.warning("   ⚠️ Não consegui ler '%s': %s", chave, exc)
                    dados[chave] = None

            for chave, id_el in IDS_DATAS.items():
                try:
                    texto = page.locator(f"#{id_el}").inner_text()
                    dados[f"ts_{chave}"] = parse_data_br(texto)
                except Exception:
                    dados[f"ts_{chave}"] = None

            agora_iso = datetime.now(timezone.utc).isoformat()
            dados["data_hora"] = dados.get("ts_nivel_atual") or agora_iso
            dados["coletado_em"] = agora_iso
            dados["usina"] = "UHE_SA_CARVALHO"
            dados["fonte"] = "cemig.com.br"

            log.info(
                "Coleta: afluência=%s defluência=%s nível=%s vol_útil=%s%%",
                dados["afluencia_m3s"],
                dados["defluencia_m3s"],
                dados["nivel_m"],
                dados["volume_util_pct"],
            )
            return dados

        except PWTimeout as exc:
            log.error("Timeout ao consultar CEMIG: %s", exc)
            return None
        except Exception as exc:
            log.error("Erro ao consultar CEMIG: %s", exc)
            return None
        finally:
            browser.close()


def gravar_no_banco(dados: dict[str, Any]) -> bool:
    """Faz upsert dos dados coletados em `historico_cemig`."""
    supabase = obter_supabase()
    chaves_internas = {"ts_nivel_atual", "ts_afluencia", "ts_defluencia"}
    payload = {k: v for k, v in dados.items() if k not in chaves_internas}

    try:
        supabase.table("historico_cemig").upsert(
            payload,
            on_conflict="usina,data_hora",
        ).execute()
        log.info("Gravado em historico_cemig: %s", payload["data_hora"])
        return True
    except Exception as exc:
        log.error("Erro no upsert: %s", exc)
        return False


def main() -> int:
    obter_supabase()
    log.info("=" * 60)
    log.info("COLETA CEMIG — UHE SÁ CARVALHO")
    log.info("=" * 60)

    dados = coletar_dados_cemig()
    if not dados:
        log.error("Falha na coleta.")
        return 1

    print("\n" + "=" * 60)
    print("DADOS COLETADOS")
    print("=" * 60)
    for k, v in dados.items():
        print(f"  {k:22s}: {v}")
    print("=" * 60)

    if not gravar_no_banco(dados):
        return 1

    log.info("=" * 60)
    log.info("Fim")
    log.info("=" * 60)
    return 0


if __name__ == "__main__":
    sys.exit(main())
