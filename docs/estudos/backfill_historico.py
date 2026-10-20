# BACKFILL DE HISTÓRICO — Popula estação nova com dados desde 2018
# Alerta Enchente CDV — Sistema de Alerta Antecipado de Cheias
#
# Autor: Ryan Lucas de Freitas Martins
# Licença: MIT

from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys
import time

# Permite importar o pacote `core` mesmo executando a partir de `docs/estudos/`
RAIZ = Path(__file__).resolve().parent.parent.parent
if str(RAIZ) not in sys.path:
    sys.path.insert(0, str(RAIZ))

from core.ana_client import (  # noqa: E402
    buscar_bloco_rest,
    extrair_medicoes_item_rest,
    normalizar_timestamp_ana,
    obter_token_ana,
)
from core.config import (  # noqa: E402
    COD_TIMOTEO,
    TZ_BRASILIA,
    configurar_logger,
    validar_env,
)
from core.db import obter_supabase  # noqa: E402

log = configurar_logger("backfill", formato_data="%H:%M:%S")

ESTACOES_BACKFILL = [
    COD_TIMOTEO,
]

DATA_INICIO_BACKFILL = datetime(2018, 8, 1, tzinfo=timezone.utc)

TAMANHO_LOTE = 1000
DIAS_POR_BLOCO = 30
PAUSA_BLOCOS = 3.0  # pausa entre blocos (segundos)
MAX_BLOCOS = 120    # segurança: no máximo 120 blocos = ~10 anos


def backfill_estacao(
    estacao: str,
    token: str,
    usuario: str,
    senha: str,
) -> int:
    """Executa backfill retroativo de 30 em 30 dias até `DATA_INICIO_BACKFILL`."""
    supabase = obter_supabase()
    log.info("Backfill: %s", estacao)
    data_busca = datetime.now(timezone.utc)
    total = 0
    blocos = 0

    while data_busca > DATA_INICIO_BACKFILL and blocos < MAX_BLOCOS:
        blocos += 1
        inicio = data_busca - timedelta(days=DIAS_POR_BLOCO)

        log.info(
            "   Bloco %s: %s a %s",
            blocos,
            inicio.astimezone(TZ_BRASILIA).strftime("%d/%m/%Y"),
            data_busca.astimezone(TZ_BRASILIA).strftime("%d/%m/%Y"),
        )

        itens, novo_token = buscar_bloco_rest(
            estacao,
            data_busca,
            token,
            usuario,
            senha,
            max_tentativas=5,
            timeout=90,
        )
        if novo_token is None:
            log.error("   Token perdido. Abortando.")
            return total
        token = novo_token

        novos = []
        for item in itens:
            dt = normalizar_timestamp_ana(item.get("Data_Hora_Medicao"))
            if dt is None or dt < DATA_INICIO_BACKFILL:
                continue

            nivel, chuva, vazao = extrair_medicoes_item_rest(item)
            if nivel is None and chuva is None and vazao is None:
                continue

            novos.append(
                {
                    "codigo_ana": estacao,
                    "data_hora": dt.isoformat(),
                    "nivel_cm": nivel,
                    "chuva_mm": chuva,
                    "vazao_m3s": vazao,
                }
            )

        if novos:
            for i in range(0, len(novos), TAMANHO_LOTE):
                lote = novos[i : i + TAMANHO_LOTE]
                try:
                    supabase.table("historico_ana").upsert(
                        lote,
                        on_conflict="codigo_ana,data_hora",
                    ).execute()
                except Exception as exc:
                    log.error("   Erro no upsert: %s", exc)
            total += len(novos)
            log.info("      %s registros", len(novos))
        else:
            log.info("      (bloco vazio)")

        data_busca = inicio
        time.sleep(PAUSA_BLOCOS)

    log.info("   %s: %s registros no total", estacao, total)
    return total


def main() -> int:
    env = validar_env("SUPABASE_URL", "SUPABASE_KEY", "ANA_USER", "ANA_PASS")
    usuario = env["ANA_USER"]
    senha = env["ANA_PASS"]

    log.info("=" * 70)
    log.info("BACKFILL DE HISTÓRICO")
    log.info("Data inicial: %s", DATA_INICIO_BACKFILL.date())
    log.info("Estações: %s", ESTACOES_BACKFILL)
    log.info("=" * 70)

    token = obter_token_ana(usuario, senha, max_tentativas=5, timeout=90)
    if not token:
        log.error("Falha na autenticação.")
        return 1

    total_geral = 0
    for est in ESTACOES_BACKFILL:
        total_geral += backfill_estacao(est, token, usuario, senha)

    log.info("=" * 70)
    log.info("Backfill concluído — %s registros no total", total_geral)
    log.info("=" * 70)
    return 0


if __name__ == "__main__":
    sys.exit(main())
