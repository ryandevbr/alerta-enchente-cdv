# COLETOR ANA VIA XML — Dados provisórios (baixa latência)
#
# Alerta Enchente CDV
#
# Consulta o endpoint SOAP legado da ANA (telemetriaws1.ana.gov.br), que
# publica dados com ~15 min de latência (vs ~60 min da API REST).
#
# Os dados entram no banco com fonte='xml' e provisorio=TRUE.
# Quando a API REST publicar o mesmo timestamp (1h depois), ela substitui
# automaticamente via upsert (fonte='rest_api', provisorio=FALSE).

import sys
from datetime import datetime, timedelta
from typing import Any

from core.ana_client import buscar_xml_ana, parse_xml_ana
from core.config import ESTACOES_ANA_XML, TZ_BRASILIA, configurar_logger
from core.db import (
    calcular_idade_minutos,
    obter_supabase,
    obter_ultima_leitura_ana,
)

log = configurar_logger("coletor_xml", formato_data="%H:%M:%S")

DIAS_RETROATIVOS = 2
LIMITE_REST_EM_DIA_MIN = 45


def gravar_via_rpc(codigo: str, registros: list[dict[str, Any]]) -> int:
    """
    Grava todos os registros em uma única chamada RPC em lote (`upsert_lote_provisorio`).
    """
    if not registros:
        return 0

    supabase = obter_supabase()
    payload = [
        {
            "codigo_ana": codigo,
            "data_hora": r["data_hora"],
            "nivel_cm": r["nivel_cm"],
            "chuva_mm": r["chuva_mm"],
            "vazao_m3s": r["vazao_m3s"],
        }
        for r in registros
    ]

    try:
        resultado = supabase.rpc(
            "upsert_lote_provisorio",
            {"p_registros": payload},
        ).execute()

        if resultado.data and len(resultado.data) > 0:
            return int(resultado.data[0].get("processados", 0))
        return 0
    except Exception as exc:
        log.error("   Erro no RPC em lote: %s", exc)
        return 0


def rest_esta_em_dia(
    codigo: str,
    limite_min: int = LIMITE_REST_EM_DIA_MIN,
) -> bool:
    """
    Verifica se o coletor REST já possui dados recentes (< `limite_min`).
    """
    leitura = obter_ultima_leitura_ana(
        codigo, apenas_com_nivel=False, fonte="rest_api"
    )
    if not leitura:
        return False

    idade_min = calcular_idade_minutos(leitura.get("data_hora"))
    if idade_min is None:
        return False

    log.info("   %s: REST com %.0f min de idade", codigo, idade_min)
    return idade_min < limite_min


def main() -> int:
    obter_supabase()
    log.info("=" * 60)
    log.info("COLETA ANA VIA XML (dados provisórios)")
    log.info("=" * 60)

    agora_brt = datetime.now(TZ_BRASILIA)
    data_fim = agora_brt.strftime("%d/%m/%Y")
    data_inicio = (agora_brt - timedelta(days=DIAS_RETROATIVOS)).strftime(
        "%d/%m/%Y"
    )

    log.info("Período: %s a %s", data_inicio, data_fim)

    total_geral = 0

    for codigo, nome in ESTACOES_ANA_XML:
        log.info("Estação: %s (%s)", nome, codigo)

        if rest_esta_em_dia(codigo):
            log.info("   REST em dia. Pulando XML.")
            continue

        log.info("   REST atrasado. Coletando XML...")

        xml_bytes = buscar_xml_ana(codigo, data_inicio, data_fim)
        if xml_bytes is None:
            log.warning("   Falha ao buscar XML")
            continue

        registros = parse_xml_ana(xml_bytes)
        log.info("   %s registros extraídos", len(registros))

        if not registros:
            continue

        registros.sort(key=lambda x: x["data_hora"])
        log.info(
            "   Faixa: %s a %s",
            registros[0]["data_hora"],
            registros[-1]["data_hora"],
        )

        inseridos = gravar_via_rpc(codigo, registros)
        log.info("   %s registros processados", inseridos)
        total_geral += inseridos

    log.info("=" * 60)
    log.info("Fim — %s registros processados", total_geral)
    log.info("=" * 60)
    return 0


if __name__ == "__main__":
    sys.exit(main())
