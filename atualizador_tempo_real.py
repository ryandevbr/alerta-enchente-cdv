# ATUALIZADOR EM TEMPO REAL — Estações Fluviométricas da ANA
# Alerta Enchente CDV
#
# Autor: Ryan Lucas de Freitas Martins
# Licença: MIT

import sys
import time
from datetime import datetime, timedelta, timezone

from core.ana_client import (
    buscar_bloco_rest,
    extrair_medicoes_item_rest,
    normalizar_timestamp_ana,
    obter_token_ana,
)
from core.config import (
    ESTACOES_ANA_COMPLETAS,
    TZ_BRASILIA,
    configurar_logger,
    validar_env,
)
from core.db import (
    obter_supabase,
    obter_ultima_leitura_ana,
    parse_iso_utc,
    registrar_sync_status,
)

log = configurar_logger("atualizador", arquivo_log="atualizador.log")

TAMANHO_LOTE_SUPABASE = 1000
DIAS_POR_BLOCO_ANA = 30
PAUSA_ENTRE_BLOCOS = 1.0
MAX_BLOCOS = 10  # segurança: no máximo 10 blocos = 300 dias


def obter_ultima_data(codigo_estacao: str) -> datetime:
    """Retorna o timestamp UTC do registro mais recente, ou 30 dias atrás."""
    leitura = obter_ultima_leitura_ana(
        codigo_estacao, apenas_com_nivel=False
    )
    if leitura and leitura.get("data_hora"):
        dt = parse_iso_utc(leitura["data_hora"])
        if dt is not None:
            return dt

    log.info("   Sem histórico. Buscando últimos 30 dias.")
    return datetime.now(timezone.utc) - timedelta(days=30)


def atualizar_estacao(
    estacao: str,
    token: str,
    usuario: str,
    senha: str,
) -> tuple[int, str]:
    """
    Sincroniza uma estação recuando em blocos de 30 dias a partir de agora
    até alcançar a última data gravada no banco.
    """
    supabase = obter_supabase()
    ultima_data = obter_ultima_data(estacao)
    agora = datetime.now(timezone.utc)

    log.info(
        "%s: último registro em %s (BRT)",
        estacao,
        ultima_data.astimezone(TZ_BRASILIA).strftime("%d/%m/%Y %H:%M"),
    )
    log.info(
        "   Gap a preencher: %.1fh",
        (agora - ultima_data).total_seconds() / 3600.0,
    )

    total_inseridos = 0
    data_busca = agora
    blocos_processados = 0

    while data_busca > ultima_data and blocos_processados < MAX_BLOCOS:
        blocos_processados += 1
        inicio_bloco = data_busca - timedelta(days=DIAS_POR_BLOCO_ANA)

        log.info(
            "   → Bloco %s: %s a %s",
            blocos_processados,
            inicio_bloco.astimezone(TZ_BRASILIA).strftime("%d/%m/%Y"),
            data_busca.astimezone(TZ_BRASILIA).strftime("%d/%m/%Y"),
        )

        itens, novo_token = buscar_bloco_rest(
            estacao, data_busca, token, usuario, senha
        )
        if novo_token:
            token = novo_token
        if not itens:
            log.info("     Bloco vazio.")

        novos = []
        for item in itens:
            dt = normalizar_timestamp_ana(item.get("Data_Hora_Medicao"))
            if dt is None or dt <= ultima_data:
                continue

            nivel, chuva, vazao = extrair_medicoes_item_rest(item)
            if nivel is None and chuva is None:
                continue

            novos.append(
                {
                    "codigo_ana": estacao,
                    "data_hora": dt.isoformat(),
                    "nivel_cm": nivel,
                    "chuva_mm": chuva,
                    "vazao_m3s": vazao,
                    "fonte": "rest_api",
                    "provisorio": False,
                }
            )

        if novos:
            for i in range(0, len(novos), TAMANHO_LOTE_SUPABASE):
                lote = novos[i : i + TAMANHO_LOTE_SUPABASE]
                try:
                    supabase.table("historico_ana").upsert(
                        lote,
                        on_conflict="codigo_ana,data_hora",
                    ).execute()
                except Exception as exc:
                    log.error("     Erro no upsert (lote %s): %s", i, exc)
                    continue

            total_inseridos += len(novos)
            log.info("     %s registros inseridos", len(novos))
        else:
            log.info("     Nenhum registro novo neste bloco")

        data_busca = inicio_bloco
        time.sleep(PAUSA_ENTRE_BLOCOS)

    if total_inseridos > 0:
        log.info("   %s: %s registros sincronizados.", estacao, total_inseridos)
    else:
        log.info("   %s: banco já estava sincronizado.", estacao)

    return total_inseridos, token


def main() -> int:
    env = validar_env("SUPABASE_URL", "SUPABASE_KEY", "ANA_USER", "ANA_PASS")
    usuario = env["ANA_USER"]
    senha = env["ANA_PASS"]

    inicio = time.time()
    log.info("=" * 60)
    log.info("SINCRONIZAÇÃO COM A ANA")
    log.info("=" * 60)

    token = obter_token_ana(usuario, senha)
    if not token:
        log.error("Falha na autenticação.")
        registrar_sync_status(0, 0, time.time() - inicio, False, "Falha na auth")
        return 1

    estacoes_ok = 0
    total_registros = 0

    for estacao in ESTACOES_ANA_COMPLETAS:
        try:
            inseridos, token = atualizar_estacao(estacao, token, usuario, senha)
            total_registros += inseridos
            estacoes_ok += 1
        except Exception as exc:
            log.exception("Erro na estação %s: %s", estacao, exc)

    duracao = time.time() - inicio
    sucesso = estacoes_ok == len(ESTACOES_ANA_COMPLETAS)

    log.info("=" * 60)
    log.info(
        "FIM — %s/%s estações | %s registros | %.1fs | %s",
        estacoes_ok,
        len(ESTACOES_ANA_COMPLETAS),
        total_registros,
        duracao,
        "OK" if sucesso else "PARCIAL",
    )
    log.info("=" * 60)

    registrar_sync_status(
        estacoes_ok,
        total_registros,
        duracao,
        sucesso,
        None if sucesso else "Alguma estação falhou",
    )
    return 0 if sucesso else 1


if __name__ == "__main__":
    sys.exit(main())
