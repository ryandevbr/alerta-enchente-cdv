# ATUALIZADOR EM TEMPO REAL — Estações Fluviométricas da ANA
# Alerta Enchente CDV
#
# Autor: Ryan Lucas de Freitas Martins
# Licença: MIT


import os
import sys
import time
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

import requests
from dotenv import load_dotenv
from supabase import create_client, Client


# CONFIGURAÇÃO

load_dotenv()

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
ANA_USER     = os.getenv("ANA_USER")
ANA_PASS     = os.getenv("ANA_PASS")

_faltando = [k for k, v in {
    "SUPABASE_URL": SUPABASE_URL, "SUPABASE_KEY": SUPABASE_KEY,
    "ANA_USER": ANA_USER, "ANA_PASS": ANA_PASS,
}.items() if not v]

if _faltando:
    print(f"Faltam variáveis: {', '.join(_faltando)}")
    sys.exit(1)

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)


# LOGGING

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.FileHandler("atualizador.log", encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
logging.getLogger("httpx").setLevel(logging.WARNING)  # silencia logs HTTP do Supabase
log = logging.getLogger("atualizador")


# CONSTANTES

ESTACOES = [
    "56696000",   # Timóteo (jusante)
    "56661000",   # Nova Era (montante principal)
    "56675080",   # UHE Guilman Jusante (montante intermediário NOVA)
    "56688080",   # Sá Carvalho Barramento (proxy operacional)
]

ANA_AUTH_URL  = "https://www.ana.gov.br/hidrowebservice/EstacoesTelemetricas/OAUth/v1"
ANA_SERIE_URL = "https://www.ana.gov.br/hidrowebservice/EstacoesTelemetricas/HidroinfoanaSerieTelemetricaDetalhada/v1"

TAMANHO_LOTE_INSUPABASE = 1000
DIAS_POR_BLOCO_ANA      = 30
PAUSA_ENTRE_BLOCOS      = 1.0
MAX_TENTATIVAS_HTTP     = 3
BACKOFF_BASE_SEGUNDOS   = 2

# Timezone oficial do Brasil (a ANA publica horários em Brasília)
TZ_BRASILIA = timezone(timedelta(hours=-3))



# AUTENTICAÇÃO

def obter_token() -> Optional[str]:
    """Solicita token JWT à ANA. Válido por 60 min (conforme manual)."""
    for tentativa in range(1, MAX_TENTATIVAS_HTTP + 1):
        try:
            resposta = requests.get(
                ANA_AUTH_URL,
                headers={"Identificador": ANA_USER, "Senha": ANA_PASS},
                timeout=30,
            )
            if resposta.status_code == 200:
                token = resposta.json().get("items", {}).get("tokenautenticacao")
                if token:
                    log.info("Token ANA obtido.")
                    return token
                log.warning("Resposta 200 sem token.")
            else:
                log.warning(f"Auth retornou {resposta.status_code} "
                            f"(tentativa {tentativa}/{MAX_TENTATIVAS_HTTP})")
        except requests.RequestException as e:
            log.warning(f"Erro de rede na auth: {e}")
        if tentativa < MAX_TENTATIVAS_HTTP:
            time.sleep(BACKOFF_BASE_SEGUNDOS ** tentativa)
    log.error("Falha ao obter token.")
    return None



# UTILITÁRIOS

def extrair_float(valor) -> Optional[float]:
    try:
        if valor is None:
            return None
        s = str(valor).strip()
        if s == "" or s.lower() in ("null", "nan", "none"):
            return None
        return float(s)
    except (ValueError, TypeError):
        return None


def normalizar_timestamp_ana(dt_str: str) -> Optional[datetime]:
    """
    A API da ANA retorna timestamps em horário de Brasília (UTC-3).
    Converte para UTC para armazenar no Supabase de forma consistente.
    """
    if not dt_str:
        return None
    try:
        limpo = dt_str.split(".")[0]
        dt = datetime.strptime(limpo, "%Y-%m-%d %H:%M:%S")
        dt = dt.replace(tzinfo=TZ_BRASILIA)
        return dt.astimezone(timezone.utc)
    except (ValueError, AttributeError):
        return None


def obter_ultima_data(codigo_estacao: str) -> datetime:
    """Retorna o timestamp UTC do registro mais recente, ou 30 dias atrás."""
    try:
        resp = (
            supabase.table("historico_ana")
            .select("data_hora")
            .eq("codigo_ana", codigo_estacao)
            .order("data_hora", desc=True)
            .limit(1)
            .execute()
        )
        if resp.data:
            dt_str = resp.data[0]["data_hora"]
            dt = datetime.fromisoformat(dt_str.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc)
        log.info(f"   Sem histórico. Buscando últimos 30 dias.")
        return datetime.now(timezone.utc) - timedelta(days=30)
    except Exception as e:
        log.error(f"   Erro ao consultar última data: {e}")
        return datetime.now(timezone.utc) - timedelta(days=30)



# MOTOR DE ATUALIZAÇÃO

def buscar_bloco(estacao: str, data_alvo: datetime, token: str) -> tuple[list, str]:
    """
    Busca um bloco de até 30 dias. O parâmetro `data_alvo` é o LIMITE SUPERIOR:
    a API retorna os 30 dias ANTERIORES a essa data.

    Retorna (itens, token_atualizado).
    """
    parametros = {
        "Código da Estação": estacao,
        "Tipo Filtro Data": "DATA_LEITURA",
        "Data de Busca (yyyy-MM-dd)": data_alvo.strftime("%Y-%m-%d"),
        "Range Intervalo de busca": "DIAS_30",
    }

    for tentativa in range(1, MAX_TENTATIVAS_HTTP + 1):
        try:
            resp = requests.get(
                ANA_SERIE_URL,
                params=parametros,
                headers={"Authorization": f"Bearer {token}"},
                timeout=60,
            )

            if resp.status_code == 401:
                log.warning("   Token expirado. Renovando...")
                novo = obter_token()
                if novo:
                    token = novo
                    continue
                log.error("   Não foi possível renovar o token.")
                return [], token

            if resp.status_code == 200:
                return resp.json().get("items", []), token

            log.warning(f"   HTTP {resp.status_code} "
                        f"(tentativa {tentativa}/{MAX_TENTATIVAS_HTTP})")

        except requests.RequestException as e:
            log.warning(f"   Erro de rede: {e}")

        if tentativa < MAX_TENTATIVAS_HTTP:
            time.sleep(BACKOFF_BASE_SEGUNDOS ** tentativa)

    return [], token


def atualizar_estacao(estacao: str, token: str) -> tuple[int, str]:
    """
    Sincroniza uma estação.

    CORREÇÃO CRÍTICA: A API da ANA retorna os N dias ANTERIORES à data
    informada, não os N dias seguintes. Portanto, o loop deve:
      1. Começar em AGORA
      2. Recuar 30 dias por vez
      3. Parar quando alcançar `ultima_data` do banco
    """
    ultima_data = obter_ultima_data(estacao)
    agora = datetime.now(timezone.utc)

    log.info(f"{estacao}: último registro em "
             f"{ultima_data.astimezone(TZ_BRASILIA).strftime('%d/%m/%Y %H:%M')} "
             f"(BRT)")
    log.info(f"   Gap a preencher: "
             f"{(agora - ultima_data).total_seconds() / 3600:.1f}h")

    total_inseridos = 0
    data_busca = agora
    blocos_processados = 0
    MAX_BLOCOS = 10  # segurança: no máximo 10 blocos = 300 dias

    while data_busca > ultima_data and blocos_processados < MAX_BLOCOS:
        blocos_processados += 1

        # Período deste bloco: [data_busca - 30d, data_busca]
        inicio_bloco = data_busca - timedelta(days=DIAS_POR_BLOCO_ANA)

        log.info(f"   → Bloco {blocos_processados}: "
                 f"{inicio_bloco.astimezone(TZ_BRASILIA).strftime('%d/%m/%Y')} "
                 f"a {data_busca.astimezone(TZ_BRASILIA).strftime('%d/%m/%Y')}")

        itens, token = buscar_bloco(estacao, data_busca, token)

        if not itens:
            log.info(f"     Bloco vazio.")

        # Filtro de ouro: só registros mais recentes que a última data
        novos = []
        for item in itens:
            dt = normalizar_timestamp_ana(item.get("Data_Hora_Medicao"))
            if dt is None or dt <= ultima_data:
                continue

            nivel = (extrair_float(item.get("Cota_Sensor"))
                     or extrair_float(item.get("Cota_Adotada")))
            chuva = (extrair_float(item.get("Chuva_Acumulada"))
                     or extrair_float(item.get("Chuva_Adotada")))
            vazao = extrair_float(item.get("Vazao_Adotada"))

            if nivel is None and chuva is None:
                continue

            novos.append({
                "codigo_ana": estacao,
                "data_hora":  dt.isoformat(),
                "nivel_cm":   nivel,
                "chuva_mm":   chuva,
                "vazao_m3s":  vazao,
                "fonte":      "rest_api",
                "provisorio": False,
            })

        # Upsert em lotes
        if novos:
            for i in range(0, len(novos), TAMANHO_LOTE_INSUPABASE):
                lote = novos[i:i + TAMANHO_LOTE_INSUPABASE]
                try:
                    supabase.table("historico_ana").upsert(
                        lote,
                        on_conflict="codigo_ana,data_hora",
                    ).execute()
                except Exception as e:
                    log.error(f"     Erro no upsert (lote {i}): {e}")
                    continue

            total_inseridos += len(novos)
            log.info(f"     {len(novos)} registros inseridos")
        else:
            log.info(f"     Nenhum registro novo neste bloco")

        # Recua 30 dias para o próximo bloco
        data_busca = inicio_bloco
        time.sleep(PAUSA_ENTRE_BLOCOS)

    if total_inseridos > 0:
        log.info(f"   {estacao}: {total_inseridos} registros sincronizados.")
    else:
        log.info(f"   {estacao}: banco já estava sincronizado.")

    return total_inseridos, token



# HEALTH CHECK

def registrar_status(estacoes_ok, total_registros, duracao, sucesso, erro=None):
    try:
        supabase.table("sync_status").insert({
            "estacoes_sincronizadas": estacoes_ok,
            "registros_inseridos":    total_registros,
            "duracao_segundos":       round(duracao, 2),
            "sucesso":                sucesso,
            "erro":                   erro,
        }).execute()
    except Exception as e:
        log.error(f"Falha ao gravar sync_status: {e}")



# ENTRYPOINT

def main() -> int:
    inicio = time.time()
    log.info("=" * 60)
    log.info("SINCRONIZAÇÃO COM A ANA")
    log.info("=" * 60)

    token = obter_token()
    if not token:
        log.error("Falha na autenticação.")
        registrar_status(0, 0, time.time() - inicio, False, "Falha na auth")
        return 1

    estacoes_ok = 0
    total_registros = 0

    for estacao in ESTACOES:
        try:
            inseridos, token = atualizar_estacao(estacao, token)
            total_registros += inseridos
            estacoes_ok += 1
        except Exception as e:
            log.exception(f"Erro na estação {estacao}: {e}")

    duracao = time.time() - inicio
    sucesso = estacoes_ok == len(ESTACOES)

    log.info("=" * 60)
    log.info(f"FIM — {estacoes_ok}/{len(ESTACOES)} estações | "
             f"{total_registros} registros | {duracao:.1f}s | "
             f"{'OK' if sucesso else 'PARCIAL'}")
    log.info("=" * 60)

    registrar_status(estacoes_ok, total_registros, duracao, sucesso,
                     None if sucesso else "Alguma estação falhou")
    return 0 if sucesso else 1


if __name__ == "__main__":
    sys.exit(main())
