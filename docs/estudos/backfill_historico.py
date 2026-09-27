# BACKFILL DE HISTÓRICO — Popula estação nova com dados desde 2018
# Alerta Enchente CDV — Sistema de Alerta Antecipado de Cheias

# Autor: Ryan Lucas de Freitas Martins
# Licença: MIT

import os
import sys
import time
import logging
from datetime import datetime, timedelta, timezone
import requests
from dotenv import load_dotenv
from supabase import create_client, Client

# CONFIGURAÇÃO

load_dotenv()

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
ANA_USER     = os.getenv("ANA_USER")
ANA_PASS     = os.getenv("ANA_PASS")

if not all([SUPABASE_URL, SUPABASE_KEY, ANA_USER, ANA_PASS]):
    print("Faltam variáveis no .env")
    sys.exit(1)

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
log = logging.getLogger("backfill")

# CONSTANTES

ESTACOES_BACKFILL = [
    "56675080",   # UHE Guilman Jusante (nível + vazão + chuva)
]

DATA_INICIO_BACKFILL = datetime(2018, 12, 1, tzinfo=timezone.utc)

ANA_AUTH_URL  = "https://www.ana.gov.br/hidrowebservice/EstacoesTelemetricas/OAUth/v1"
ANA_SERIE_URL = "https://www.ana.gov.br/hidrowebservice/EstacoesTelemetricas/HidroinfoanaSerieTelemetricaDetalhada/v1"

TAMANHO_LOTE   = 1000
DIAS_POR_BLOCO = 30
PAUSA_BLOCOS   = 3.0     # pausa entre blocos (segundos)
MAX_BLOCOS     = 120     # segurança: no máximo 120 blocos = ~10 anos

TZ_BRASILIA = timezone(timedelta(hours=-3))

# AUTENTICAÇÃO

def obter_token():
    """Autentica na ANA com retry e backoff para erros 5xx."""
    log.info("Autenticando na ANA...")
    for tentativa in range(1, 6):
        try:
            r = requests.get(
                ANA_AUTH_URL,
                headers={"Identificador": ANA_USER, "Senha": ANA_PASS},
                timeout=90,
            )
            log.info(f"   Tentativa {tentativa}: HTTP {r.status_code}")

            if r.status_code == 200:
                tok = r.json().get("items", {}).get("tokenautenticacao")
                if tok:
                    log.info(f"   Token obtido ({len(tok)} chars)")
                    return tok
                log.error(f"   Resposta sem token: {r.text[:200]}")
                return None

            if r.status_code == 401:
                log.error("   Credenciais inválidas.")
                return None

            if r.status_code in (502, 503, 504):
                log.warning(f"   ⏸ ANA sobrecarregada ({r.status_code}). "
                            f"Aguardando 60s...")
                time.sleep(60)
                continue

            log.warning(f"   HTTP {r.status_code} — retry em 30s")

        except requests.RequestException as e:
            log.warning(f"   ⚠️ {type(e).__name__}: {e}")

        time.sleep(30)

    log.error("Falha na autenticação após 5 tentativas.")
    return None

# UTILITÁRIOS

def extrair_float(valor):
    """Converte string/None para float, retornando None em falha."""
    try:
        if valor is None:
            return None
        s = str(valor).strip()
        if s == "" or s.lower() in ("null", "nan", "none"):
            return None
        return float(s)
    except (ValueError, TypeError):
        return None

def normalizar_ts(dt_str):
    """Converte timestamp da ANA (BRT) para UTC."""
    if not dt_str:
        return None
    try:
        limpo = dt_str.split(".")[0]
        dt = datetime.strptime(limpo, "%Y-%m-%d %H:%M:%S")
        return dt.replace(tzinfo=TZ_BRASILIA).astimezone(timezone.utc)
    except (ValueError, AttributeError):
        return None

# BUSCA DE BLOCO

def buscar_bloco(estacao, data_alvo, token):
    """
    Busca um bloco de 30 dias. O parâmetro `data_alvo` é o LIMITE SUPERIOR:
    a API retorna os 30 dias ANTERIORES a essa data.

    Retorna (itens, token_atualizado).
    """
    params = {
        "Código da Estação": estacao,
        "Tipo Filtro Data": "DATA_LEITURA",
        "Data de Busca (yyyy-MM-dd)": data_alvo.strftime("%Y-%m-%d"),
        "Range Intervalo de busca": "DIAS_30",
    }

    for tentativa in range(1, 6):
        try:
            r = requests.get(
                ANA_SERIE_URL,
                params=params,
                headers={"Authorization": f"Bearer {token}"},
                timeout=90,
            )

            if r.status_code == 401:
                log.warning("   Token expirado, renovando...")
                token = obter_token()
                if not token:
                    return [], None
                continue

            if r.status_code == 200:
                return r.json().get("items", []), token

            if r.status_code in (502, 503, 504):
                log.warning(f"   ⏸ ANA retornou {r.status_code}. "
                            f"Aguardando 60s...")
                time.sleep(60)
                continue

            log.warning(f"   ⚠️ HTTP {r.status_code} (tentativa {tentativa})")

        except requests.RequestException as e:
            log.warning(f"   ⚠️ {type(e).__name__}: {e}")

        time.sleep(2 ** tentativa)

    return [], token

# BACKFILL DE UMA ESTAÇÃO

def backfill_estacao(estacao, token):
    log.info(f"Backfill: {estacao}")
    agora = datetime.now(timezone.utc)
    data_busca = agora
    total = 0
    blocos = 0

    while data_busca > DATA_INICIO_BACKFILL and blocos < MAX_BLOCOS:
        blocos += 1
        inicio = data_busca - timedelta(days=DIAS_POR_BLOCO)

        log.info(f"   Bloco {blocos}: "
                 f"{inicio.astimezone(TZ_BRASILIA).strftime('%d/%m/%Y')} a "
                 f"{data_busca.astimezone(TZ_BRASILIA).strftime('%d/%m/%Y')}")

        itens, token = buscar_bloco(estacao, data_busca, token)
        if token is None:
            log.error("   Token perdido. Abortando.")
            return total

        # Filtra e normaliza registros
        novos = []
        for item in itens:
            dt = normalizar_ts(item.get("Data_Hora_Medicao"))
            if dt is None:
                continue
            if dt < DATA_INICIO_BACKFILL:
                continue

            nivel = (extrair_float(item.get("Cota_Sensor"))
                     or extrair_float(item.get("Cota_Adotada")))
            chuva = (extrair_float(item.get("Chuva_Acumulada"))
                     or extrair_float(item.get("Chuva_Adotada")))
            vazao = extrair_float(item.get("Vazao_Adotada"))

            if nivel is None and chuva is None and vazao is None:
                continue

            novos.append({
                "codigo_ana": estacao,
                "data_hora":  dt.isoformat(),
                "nivel_cm":   nivel,
                "chuva_mm":   chuva,
                "vazao_m3s":  vazao,
            })

        # Upsert em lotes
        if novos:
            for i in range(0, len(novos), TAMANHO_LOTE):
                lote = novos[i:i + TAMANHO_LOTE]
                try:
                    supabase.table("historico_ana").upsert(
                        lote,
                        on_conflict="codigo_ana,data_hora",
                    ).execute()
                except Exception as e:
                    log.error(f"   Erro no upsert: {e}")
            total += len(novos)
            log.info(f"      {len(novos)} registros")
        else:
            log.info(f"      (bloco vazio)")

        data_busca = inicio
        time.sleep(PAUSA_BLOCOS)

    log.info(f"   {estacao}: {total} registros no total")
    return total

# MAIN

def main():
    log.info("=" * 70)
    log.info("BACKFILL DE HISTÓRICO")
    log.info(f"Data inicial: {DATA_INICIO_BACKFILL.date()}")
    log.info(f"Estações: {ESTACOES_BACKFILL}")
    log.info("=" * 70)

    token = obter_token()
    if not token:
        log.error("Falha na autenticação.")
        return 1

    total_geral = 0
    for est in ESTACOES_BACKFILL:
        total_geral += backfill_estacao(est, token)

    log.info("=" * 70)
    log.info(f"Backfill concluído — {total_geral} registros no total")
    log.info("=" * 70)
    return 0

if __name__ == "__main__":
    sys.exit(main())