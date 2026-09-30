# COLETOR ANA VIA XML — Dados provisorios (baixa latencia)
#
# Alerta Enchente CDV
#
# Consulta o endpoint SOAP legado da ANA (telemetriaws1.ana.gov.br), que
# publica dados com ~15 min de latencia (vs ~60 min da API REST).
#
# Os dados entram no banco com fonte='xml' e provisorio=TRUE.
# Quando a API REST publicar o mesmo timestamp (1h depois), ela substitui
# automaticamente via upsert (fonte='rest_api', provisorio=FALSE).


import os
import sys
import time
import logging
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from typing import Optional

import requests
from dotenv import load_dotenv
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
log = logging.getLogger("coletor_xml")

TZ_BRASILIA = timezone(timedelta(hours=-3))

XML_URL = "http://telemetriaws1.ana.gov.br/ServiceANA.asmx/DadosHidrometeorologicos"

# Estacoes que vamos coletar via XML
ESTACOES = [
    ("56661000", "Nova Era"),
    ("56696000", "Timoteo"),
]

# Busca os ultimos 2 dias
DIAS_RETROATIVOS = 2

MAX_TENTATIVAS_HTTP = 3
BACKOFF_BASE_SEGUNDOS = 2

# HELPERS

def normalizar_ts(dt_str: str) -> Optional[str]:
    """
    Converte '2026-09-29 18:30:00 ' (BRT) para ISO-8601 UTC.
    """
    if not dt_str:
        return None
    try:
        limpo = dt_str.strip().split(".")[0]
        dt = datetime.strptime(limpo, "%Y-%m-%d %H:%M:%S")
        dt = dt.replace(tzinfo=TZ_BRASILIA).astimezone(timezone.utc)
        return dt.replace(microsecond=0).isoformat()
    except (ValueError, AttributeError):
        return None


def extrair_float(valor: Optional[str]) -> Optional[float]:
    if valor is None:
        return None
    s = str(valor).strip()
    if s == "" or s.lower() in ("null", "nan", "none"):
        return None
    try:
        return float(s)
    except (ValueError, TypeError):
        return None


def strip_namespace(elem):
    """Remove prefixos de namespace dos elementos."""
    for el in elem.iter():
        if "}" in el.tag:
            el.tag = el.tag.split("}", 1)[1]
    return elem


# CONSULTA XML

def buscar_xml(codigo: str, data_inicio: str, data_fim: str) -> Optional[bytes]:
    """
    Consulta o endpoint XML. Retorna o conteudo cru em bytes.
    """
    params = {
        "codEstacao": codigo,
        "dataInicio": data_inicio,
        "dataFim":    data_fim,
    }

    for tentativa in range(1, MAX_TENTATIVAS_HTTP + 1):
        try:
            r = requests.get(XML_URL, params=params, timeout=60)
            if r.status_code == 200:
                return r.content
            log.warning(f"   HTTP {r.status_code} (tentativa {tentativa})")
        except requests.RequestException as e:
            log.warning(f"   Erro de rede: {e} (tentativa {tentativa})")

        if tentativa < MAX_TENTATIVAS_HTTP:
            time.sleep(BACKOFF_BASE_SEGUNDOS ** tentativa)

    return None


def parse_xml(xml_bytes: bytes) -> list[dict]:
    """
    Extrai os registros do XML e devolve lista de dicts no formato
    aceito pelo Supabase.
    """
    try:
        root = ET.fromstring(xml_bytes)
        root = strip_namespace(root)
    except ET.ParseError as e:
        log.error(f"   Erro de parse XML: {e}")
        return []

    registros = []
    for elem in root.iter("DadosHidrometereologicos"):
        dt_raw = elem.findtext("DataHora")
        dt_iso = normalizar_ts(dt_raw)
        if dt_iso is None:
            continue

        nivel = extrair_float(elem.findtext("Nivel"))
        chuva = extrair_float(elem.findtext("Chuva"))
        vazao = extrair_float(elem.findtext("Vazao"))

        if nivel is None and chuva is None:
            continue

        registros.append({
            "data_hora":  dt_iso,
            "nivel_cm":   nivel,
            "chuva_mm":   chuva,
            "vazao_m3s":  vazao,
        })

    return registros


# PERSISTENCIA

def gravar_via_rpc(codigo: str, registros: list[dict]) -> int:
    """
    Grava todos os registros em uma única chamada RPC em lote.
    Reduz de ~270 chamadas para 1.
    """
    if not registros:
        return 0

    # Adiciona o código da estação em cada registro
    payload = [
        {
            "codigo_ana": codigo,
            "data_hora":  r["data_hora"],
            "nivel_cm":   r["nivel_cm"],
            "chuva_mm":   r["chuva_mm"],
            "vazao_m3s":  r["vazao_m3s"],
        }
        for r in registros
    ]

    try:
        resultado = supabase.rpc("upsert_lote_provisorio", {
            "p_registros": payload,
        }).execute()

        if resultado.data and len(resultado.data) > 0:
            return resultado.data[0].get("processados", 0)
        return 0
    except Exception as e:
        log.error(f"   Erro no RPC em lote: {e}")
        return 0

def rest_esta_em_dia(codigo: str, limite_min: int = 45) -> bool:
    """
    Verifica se o REST já tem dados recentes.
    Se a última leitura for menor que `limite_min`, considera em dia.
    """
    try:
        r = (supabase.table("historico_ana")
             .select("data_hora")
             .eq("codigo_ana", codigo)
             .eq("fonte", "rest_api")
             .order("data_hora", desc=True)
             .limit(1)
             .execute())

        if not r.data:
            return False

        ultima = datetime.fromisoformat(r.data[0]["data_hora"].replace("Z", "+00:00"))
        idade_min = (datetime.now(timezone.utc) - ultima).total_seconds() / 60

        log.info(f"   {codigo}: REST com {idade_min:.0f} min de idade")
        return idade_min < limite_min

    except Exception as e:
        log.warning(f"   Erro ao verificar REST: {e}")
        return False

# MAIN

def main() -> int:
    log.info("=" * 60)
    log.info("COLETA ANA VIA XML (dados provisorios)")
    log.info("=" * 60)

    agora_brt = datetime.now(TZ_BRASILIA)
    data_fim = agora_brt.strftime("%d/%m/%Y")
    data_inicio = (agora_brt - timedelta(days=DIAS_RETROATIVOS)).strftime("%d/%m/%Y")

    log.info(f"Periodo: {data_inicio} a {data_fim}")

    total_geral = 0

    for codigo, nome in ESTACOES:
        log.info(f"Estacao: {nome} ({codigo})")

        # Se o REST já está em dia, o XML não tem nada a adicionar
        if rest_esta_em_dia(codigo, limite_min=45):
            log.info(f"   REST em dia. Pulando XML.")
            continue

        log.info(f"   REST atrasado. Coletando XML...")

        xml = buscar_xml(codigo, data_inicio, data_fim)
        if xml is None:
            log.warning(f"   Falha ao buscar XML")
            continue

        registros = parse_xml(xml)
        log.info(f"   {len(registros)} registros extraidos")

        if not registros:
            continue

        registros.sort(key=lambda x: x["data_hora"])
        log.info(f"   Faixa: {registros[0]['data_hora']} a {registros[-1]['data_hora']}")

        inseridos = gravar_via_rpc(codigo, registros)
        log.info(f"   {inseridos} registros processados")
        total_geral += inseridos

    log.info("=" * 60)
    log.info(f"Fim — {total_geral} registros processados")
    log.info("=" * 60)
    return 0


if __name__ == "__main__":
    sys.exit(main())