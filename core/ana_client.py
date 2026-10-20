"""Cliente unificado da ANA (API REST HidroWeb e SOAP/XML) e parsers de dados."""

import logging
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from typing import Any, Optional, TypeVar

import requests

from core.config import TZ_BRASILIA

log = logging.getLogger(__name__)

ANA_AUTH_URL = (
    "https://www.ana.gov.br/hidrowebservice/EstacoesTelemetricas/OAUth/v1"
)
ANA_SERIE_URL = (
    "https://www.ana.gov.br/hidrowebservice/EstacoesTelemetricas/"
    "HidroinfoanaSerieTelemetricaDetalhada/v1"
)
ANA_XML_URL = (
    "http://telemetriaws1.ana.gov.br/ServiceANA.asmx/DadosHidrometeorologicos"
)

MAX_TENTATIVAS_HTTP = 3
BACKOFF_BASE_SEGUNDOS = 2

T = TypeVar("T")

_session: Optional[requests.Session] = None


def obter_sessao_http() -> requests.Session:
    """Retorna uma sessão HTTP reutilizável (connection pooling)."""
    global _session
    if _session is None:
        _session = requests.Session()
    return _session


def primeiro_nao_nulo(*valores: Optional[T]) -> Optional[T]:
    """
    Retorna o primeiro valor que não seja None.
    Importante: preserva `0.0` (evita o bug do operador `or` com floats zero).
    """
    for valor in valores:
        if valor is not None:
            return valor
    return None


def extrair_float(valor: Any) -> Optional[float]:
    """Converte string/número para float, retornando None se vazio ou inválido."""
    if valor is None:
        return None
    s = str(valor).strip()
    if not s or s.lower() in ("null", "nan", "none"):
        return None
    try:
        return float(s)
    except (ValueError, TypeError):
        return None


def normalizar_timestamp_ana(dt_str: Optional[str]) -> Optional[datetime]:
    """
    A API da ANA retorna timestamps em horário de Brasília (UTC-3).
    Converte para datetime em UTC (sem microssegundos).
    """
    if not dt_str:
        return None
    try:
        limpo = dt_str.strip().split(".")[0]
        dt = datetime.strptime(limpo, "%Y-%m-%d %H:%M:%S")
        return dt.replace(tzinfo=TZ_BRASILIA).astimezone(timezone.utc)
    except (ValueError, AttributeError):
        return None


def normalizar_timestamp_iso(dt_str: Optional[str]) -> Optional[str]:
    """Converte string de data/hora da ANA (BRT) para ISO-8601 UTC."""
    dt = normalizar_timestamp_ana(dt_str)
    return dt.replace(microsecond=0).isoformat() if dt else None


def extrair_medicoes_item_rest(item: dict[str, Any]) -> tuple[
    Optional[float], Optional[float], Optional[float]
]:
    """
    Extrai (nivel_cm, chuva_mm, vazao_m3s) de um item JSON da API REST da ANA,
    preservando valores iguais a 0.0.
    """
    nivel = primeiro_nao_nulo(
        extrair_float(item.get("Cota_Sensor")),
        extrair_float(item.get("Cota_Adotada")),
    )
    chuva = primeiro_nao_nulo(
        extrair_float(item.get("Chuva_Acumulada")),
        extrair_float(item.get("Chuva_Adotada")),
    )
    vazao = extrair_float(item.get("Vazao_Adotada"))
    return nivel, chuva, vazao


def obter_token_ana(
    usuario: str,
    senha: str,
    max_tentativas: int = MAX_TENTATIVAS_HTTP,
    timeout: int = 45,
) -> Optional[str]:
    """Solicita token JWT à ANA (válido por 60 min) com retry e backoff."""
    sessao = obter_sessao_http()

    for tentativa in range(1, max_tentativas + 1):
        try:
            resposta = sessao.get(
                ANA_AUTH_URL,
                headers={"Identificador": usuario, "Senha": senha},
                timeout=timeout,
            )
            if resposta.status_code == 200:
                token = resposta.json().get("items", {}).get("tokenautenticacao")
                if token:
                    log.info("Token ANA obtido.")
                    return token
                log.warning("Resposta 200 sem token.")
            elif resposta.status_code == 401:
                log.error("Credenciais da ANA inválidas (HTTP 401).")
                return None
            elif resposta.status_code in (502, 503, 504):
                espera = 30 if tentativa < max_tentativas else 0
                log.warning(
                    "ANA sobrecarregada (HTTP %s). Aguardando %ss...",
                    resposta.status_code,
                    espera,
                )
                if espera:
                    time.sleep(espera)
                continue
            else:
                log.warning(
                    "Auth retornou HTTP %s (tentativa %s/%s)",
                    resposta.status_code,
                    tentativa,
                    max_tentativas,
                )
        except requests.RequestException as exc:
            log.warning("Erro de rede na auth ANA: %s", exc)

        if tentativa < max_tentativas:
            time.sleep(BACKOFF_BASE_SEGUNDOS**tentativa)

    log.error("Falha ao obter token da ANA.")
    return None


def buscar_bloco_rest(
    estacao: str,
    data_alvo: datetime,
    token: str,
    usuario: str,
    senha: str,
    max_tentativas: int = MAX_TENTATIVAS_HTTP,
    timeout: int = 60,
) -> tuple[list[dict[str, Any]], Optional[str]]:
    """
    Busca um bloco de até 30 dias anteriores a `data_alvo` na API REST da ANA.
    Renova automaticamente o token em caso de HTTP 401.
    Retorna (itens, token_atualizado).
    """
    sessao = obter_sessao_http()
    parametros = {
        "Código da Estação": estacao,
        "Tipo Filtro Data": "DATA_LEITURA",
        "Data de Busca (yyyy-MM-dd)": data_alvo.strftime("%Y-%m-%d"),
        "Range Intervalo de busca": "DIAS_30",
    }

    for tentativa in range(1, max_tentativas + 1):
        try:
            resp = sessao.get(
                ANA_SERIE_URL,
                params=parametros,
                headers={"Authorization": f"Bearer {token}"},
                timeout=timeout,
            )

            if resp.status_code == 401:
                log.warning("   Token expirado. Renovando...")
                novo_token = obter_token_ana(
                    usuario, senha, max_tentativas=max_tentativas
                )
                if novo_token:
                    token = novo_token
                    continue
                log.error("   Não foi possível renovar o token.")
                return [], None

            if resp.status_code == 200:
                return resp.json().get("items", []) or [], token

            if resp.status_code in (502, 503, 504) and tentativa < max_tentativas:
                log.warning(
                    "   ANA retornou HTTP %s. Aguardando 30s...",
                    resp.status_code,
                )
                time.sleep(30)
                continue

            log.warning(
                "   HTTP %s (tentativa %s/%s)",
                resp.status_code,
                tentativa,
                max_tentativas,
            )
        except requests.RequestException as exc:
            log.warning("   Erro de rede: %s", exc)

        if tentativa < max_tentativas:
            time.sleep(BACKOFF_BASE_SEGUNDOS**tentativa)

    return [], token


def buscar_xml_ana(
    codigo: str,
    data_inicio: str,
    data_fim: str,
    max_tentativas: int = MAX_TENTATIVAS_HTTP,
) -> Optional[bytes]:
    """Consulta o endpoint SOAP/XML legado da ANA e retorna os bytes crus."""
    sessao = obter_sessao_http()
    params = {
        "codEstacao": codigo,
        "dataInicio": data_inicio,
        "dataFim": data_fim,
    }

    for tentativa in range(1, max_tentativas + 1):
        try:
            resp = sessao.get(ANA_XML_URL, params=params, timeout=60)
            if resp.status_code == 200:
                return resp.content
            log.warning("   HTTP %s (tentativa %s)", resp.status_code, tentativa)
        except requests.RequestException as exc:
            log.warning("   Erro de rede: %s (tentativa %s)", exc, tentativa)

        if tentativa < max_tentativas:
            time.sleep(BACKOFF_BASE_SEGUNDOS**tentativa)

    return None


def _remover_namespaces_xml(elem: ET.Element) -> ET.Element:
    """Remove prefixos de namespace das tags XML in-place."""
    for el in elem.iter():
        if "}" in el.tag:
            el.tag = el.tag.split("}", 1)[1]
    return elem


def parse_xml_ana(xml_bytes: bytes) -> list[dict[str, Any]]:
    """Extrai os registros hidrometeorológicos do XML da ANA."""
    try:
        root = ET.fromstring(xml_bytes)
        root = _remover_namespaces_xml(root)
    except ET.ParseError as exc:
        log.error("   Erro de parse XML: %s", exc)
        return []

    registros: list[dict[str, Any]] = []
    for elem in root.iter("DadosHidrometereologicos"):
        dt_iso = normalizar_timestamp_iso(elem.findtext("DataHora"))
        if dt_iso is None:
            continue

        nivel = extrair_float(elem.findtext("Nivel"))
        chuva = extrair_float(elem.findtext("Chuva"))
        vazao = extrair_float(elem.findtext("Vazao"))

        if nivel is None and chuva is None:
            continue

        registros.append(
            {
                "data_hora": dt_iso,
                "nivel_cm": nivel,
                "chuva_mm": chuva,
                "vazao_m3s": vazao,
            }
        )

    return registros

