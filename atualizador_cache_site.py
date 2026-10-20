# ATUALIZADOR DE CACHE — Site Público Alerta Enchente CDV
#
# Lê `historico_ana` e `historico_cemig` e produz os objetos JSON consumidos
# pelo frontend em `cache_site`:
#   - status_atual       (Timóteo agora + UHE Sá Carvalho)
#   - onda_desfasada     (previsão por variação de Nova Era, +1h a +9h)
#   - comparativo_anual  (mesmo dia/hora desde 2018)
#   - defluencia_48h     (série CEMIG 48h para sparkline)
#   - historico_rio_7d   (série horária 7 dias)
#   - camera_status      (estado da transmissão ao vivo MediaMTX)
#
# Autor: Ryan Lucas de Freitas Martins
# Licença: MIT

import sys
import time
from typing import Optional

from core.cache_builders import (
    construir_comparativo_anual,
    construir_defluencia_48h,
    construir_onda_desfasada,
    gerar_historico_rio_7d,
    obter_status_atual,
    obter_status_camera,
)
from core.config import LAG_HORAS, configurar_logger
from core.db import gravar_cache_site, obter_supabase, registrar_sync_status

log = configurar_logger("cache", arquivo_log="atualizador_cache.log")


def _registrar_status_cache(
    sucesso: bool,
    duracao: float = 0.0,
    erro: Optional[str] = None,
) -> None:
    registrar_sync_status(
        estacoes_sincronizadas=2 if sucesso else 0,
        registros_inseridos=0,
        duracao_segundos=duracao,
        sucesso=sucesso,
        erro=f"[cache] {erro}" if erro else "[cache] ok",
        silencioso=True,
    )


def main() -> int:
    inicio = time.time()
    supabase = obter_supabase()

    log.info("=" * 60)
    log.info("ATUALIZANDO CACHE DO SITE PÚBLICO")
    log.info("   Lag configurado: %sh", LAG_HORAS)
    log.info("=" * 60)

    # ---- 1. Status atual (Timóteo + CEMIG) ----
    status = obter_status_atual(client=supabase)
    if status:
        if gravar_cache_site("status_atual", status, client=supabase):
            log.info(
                "status_atual → %s cm (%s)",
                status["nivel_atual"],
                status["ultima_leitura"],
            )
        else:
            _registrar_status_cache(
                False, time.time() - inicio, "falha ao gravar status_atual"
            )
            return 1
    else:
        log.warning("status_atual não foi atualizado (sem dados)")

    # ---- 2. Onda desfasada (Nova Era) ----
    nivel_atual = status["nivel_atual"] if status else None
    onda = construir_onda_desfasada(nivel_atual, client=supabase)
    if onda:
        if gravar_cache_site("onda_desfasada", onda, client=supabase):
            resumo = ", ".join(f"{k}h:{v}" for k, v in sorted(onda.items()))
            log.info("onda_desfasada → %s", resumo)
        else:
            log.warning("falha ao gravar onda_desfasada")
    else:
        log.warning("onda_desfasada não foi atualizada")

    # ---- 3. Comparativo anual ----
    comparativo = construir_comparativo_anual(
        status_atual=status, client=supabase
    )
    if comparativo:
        if gravar_cache_site("comparativo_anual", comparativo, client=supabase):
            n = len(comparativo["anos_anteriores"])
            est = comparativo["estatisticas"]
            log.info(
                "comparativo_anual → %s anos | média=%s cm | atual=%s cm",
                n,
                est["media"],
                comparativo["atual"]["nivel_cm"],
            )
        else:
            log.warning("falha ao gravar comparativo_anual")
    else:
        log.warning("comparativo_anual não foi atualizado")

    # ---- 4. Defluência 48h ----
    defluencia = construir_defluencia_48h(client=supabase)
    if defluencia:
        if gravar_cache_site("defluencia_48h", defluencia, client=supabase):
            log.info(
                "defluencia_48h → %s pontos (%s–%s m³/s)",
                len(defluencia["pontos"]),
                defluencia["min"],
                defluencia["max"],
            )
        else:
            log.warning("falha ao gravar defluencia_48h")
    else:
        log.warning("defluencia_48h não foi atualizada")

    # ---- 5. Histórico do rio 7 dias ----
    historico = gerar_historico_rio_7d(client=supabase)
    if historico:
        if gravar_cache_site("historico_rio_7d", historico, client=supabase):
            log.info(
                "historico_rio_7d → %s pontos (%s–%s cm) | tendência 24h: %s cm",
                len(historico["pontos"]),
                historico["min"],
                historico["max"],
                historico["tendencia_24h"],
            )
        else:
            log.warning("falha ao gravar historico_rio_7d")
    else:
        log.warning("historico_rio_7d não foi atualizado")

    # ---- 6. Status da câmera ao vivo ----
    camera = obter_status_camera()
    if camera:
        if gravar_cache_site("camera_status", camera, client=supabase):
            estado = "AO VIVO" if camera["ao_vivo"] else "offline"
            log.info("camera_status → %s", estado)
        else:
            log.warning("falha ao gravar camera_status")
    else:
        log.warning("camera_status não foi atualizado")

    duracao = time.time() - inicio
    log.info("=" * 60)
    log.info("Cache atualizado com sucesso (%.1fs)", duracao)
    log.info("=" * 60)

    _registrar_status_cache(True, duracao)
    return 0


if __name__ == "__main__":
    sys.exit(main())
