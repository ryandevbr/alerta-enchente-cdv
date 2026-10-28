"""Construtores dos objetos de cache consumidos pelo site público (`cache_site`)."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import logging
from statistics import mean, median
from typing import TYPE_CHECKING, Any, Optional

import pandas as pd

if TYPE_CHECKING:
    from supabase import Client

from core.ana_client import extrair_float, obter_sessao_http
from core.config import COD_NOVA_ERA, COD_TIMOTEO, LAG_HORAS, VPS_CAMERA_URL
from core.db import (
    obter_supabase,
    obter_ultima_leitura_ana,
    obter_ultima_leitura_cemig,
    parse_iso_utc,
)

log = logging.getLogger("cache")

# Tolerância (em horas) para encontrar uma leitura próxima do alvo na série da ANA.
TOLERANCIA_BUSCA_HORAS = 2
TOLERANCIA_COMPARATIVO_HORAS = 3


def obter_status_atual(client: Optional[Client] = None) -> Optional[dict[str, Any]]:
    """
    Retorna o nível mais recente de Timóteo + dados CEMIG (se disponíveis).
    """
    db = client or obter_supabase()
    leitura_timoteo = obter_ultima_leitura_ana(
        COD_TIMOTEO, apenas_com_nivel=True, client=db
    )
    if not leitura_timoteo:
        log.warning("Sem dados recentes de Timóteo em historico_ana")
        return None

    status: dict[str, Any] = {
        "nivel_atual": round(float(leitura_timoteo["nivel_cm"]), 1),
        "ultima_leitura": leitura_timoteo["data_hora"],
        "defluencia_cemig": None,
        "afluencia_cemig": None,
        "volume_util_cemig": None,
        "cemig_atualizado_em": None,
    }

    leitura_cemig = obter_ultima_leitura_cemig(apenas_com_defluencia=True, client=db)
    if leitura_cemig:
        status["defluencia_cemig"] = leitura_cemig.get("defluencia_m3s")
        status["afluencia_cemig"] = leitura_cemig.get("afluencia_m3s")
        status["volume_util_cemig"] = leitura_cemig.get("volume_util_pct")
        status["cemig_atualizado_em"] = leitura_cemig.get("data_hora")

    return status


def obter_serie_montante(
    horas_atras: int,
    client: Optional[Client] = None,
) -> Optional[pd.DataFrame]:
    """
    Retorna DataFrame horário (resample 1h, ffill) com a série recente de
    Nova Era. Índice em UTC. Coluna: nivel_cm.
    """
    db = client or obter_supabase()
    agora = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    desde = agora - timedelta(hours=horas_atras + 2)

    try:
        resp = (
            db.table("historico_ana")
            .select("data_hora,nivel_cm")
            .eq("codigo_ana", COD_NOVA_ERA)
            .gte("data_hora", desde.isoformat())
            .not_.is_("nivel_cm", "null")
            .order("data_hora")
            .execute()
        )
    except Exception as exc:
        log.error("Erro ao consultar Nova Era: %s", exc)
        return None

    if not resp.data:
        log.warning("Sem dados de Nova Era nas últimas %sh", horas_atras + 2)
        return None

    df = pd.DataFrame(resp.data)
    df["data_hora"] = pd.to_datetime(df["data_hora"], utc=True)
    df = df.set_index("data_hora").sort_index()

    # Reamostra para hora cheia (pega a mais recente de cada hora)
    return df.resample("1h").last().ffill()


def construir_onda_desfasada(
    nivel_timoteo_atual: Optional[float],
    client: Optional[Client] = None,
) -> Optional[dict[int, float]]:
    """
    Monta `{1: previsao_T, ..., LAG: previsao_T}` usando o método da variação
    (delta) de Nova Era aplicada sobre o nível atual de Timóteo:
        delta(n) = NE(agora - LAG + n) - NE(agora - LAG)
        onda[n]  = T_agora + delta(n)
    """
    if nivel_timoteo_atual is None:
        log.warning("Nível de Timóteo indisponível. Onda não calculada.")
        return None

    df = obter_serie_montante(LAG_HORAS, client=client)
    if df is None or df.empty:
        return None

    agora = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    alvo_base = agora - timedelta(hours=LAG_HORAS)
    tol = timedelta(hours=TOLERANCIA_BUSCA_HORAS)

    janela_base = df[(df.index >= alvo_base - tol) & (df.index <= alvo_base + tol)]
    if janela_base.empty:
        log.warning("Sem leitura de Nova Era em ~%s. Onda não calculada.", alvo_base)
        return None

    pos_base = abs((janela_base.index - alvo_base).total_seconds()).argmin()
    ne_base = janela_base.iloc[pos_base]["nivel_cm"]
    if pd.isna(ne_base):
        log.warning("NE base inválida.")
        return None

    onda: dict[int, float] = {}
    for n in range(1, LAG_HORAS + 1):
        alvo_n = agora - timedelta(hours=LAG_HORAS - n)
        janela_n = df[(df.index >= alvo_n - tol) & (df.index <= alvo_n + tol)]
        if janela_n.empty:
            continue

        pos_n = abs((janela_n.index - alvo_n).total_seconds()).argmin()
        ne_n = janela_n.iloc[pos_n]["nivel_cm"]
        if pd.isna(ne_n):
            continue

        delta = ne_n - ne_base
        onda[n] = round(float(nivel_timoteo_atual + delta), 1)

    if not onda:
        log.warning("Nenhum valor calculado para a onda.")
        return None

    return onda


def _buscar_nivel_ano_anterior(
    ano: int,
    ref_dt: datetime,
    db: Client,
) -> Optional[dict[str, Any]]:
    """Busca o registro de Timóteo mais próximo de `ref_dt` em um `ano` específico."""
    try:
        alvo = ref_dt.replace(year=ano)
    except ValueError:
        # 29/02 em ano não bissexto
        return None

    janela_ini = (alvo - timedelta(hours=TOLERANCIA_COMPARATIVO_HORAS)).isoformat()
    janela_fim = (alvo + timedelta(hours=TOLERANCIA_COMPARATIVO_HORAS)).isoformat()

    try:
        resp = (
            db.table("historico_ana")
            .select("data_hora,nivel_cm")
            .eq("codigo_ana", COD_TIMOTEO)
            .not_.is_("nivel_cm", "null")
            .gte("data_hora", janela_ini)
            .lte("data_hora", janela_fim)
            .order("data_hora")
            .limit(50)
            .execute()
        )
    except Exception as exc:
        log.warning("   Ano %s: erro na consulta (%s)", ano, exc)
        return None

    if not resp.data:
        return None

    def _diff_segundos(item: dict[str, Any]) -> float:
        dt = parse_iso_utc(item["data_hora"])
        return abs((dt - alvo).total_seconds()) if dt else float("inf")

    mais_proximo = min(resp.data, key=_diff_segundos)
    nivel = extrair_float(mais_proximo.get("nivel_cm"))
    if nivel is None:
        return None

    return {"ano": ano, "nivel_cm": round(nivel, 1)}


def construir_comparativo_anual(
    status_atual: Optional[dict[str, Any]] = None,
    client: Optional[Client] = None,
) -> Optional[dict[str, Any]]:
    """
    Consulta o histórico de Timóteo no mesmo dia/hora nos anos anteriores (desde 2018)
    e calcula estatísticas (média, mediana, min, max).
    Reutiliza `status_atual` (se fornecido) e paraleliza as queries históricas.
    """
    db = client or obter_supabase()
    try:
        if status_atual and status_atual.get("ultima_leitura"):
            ref_ts = status_atual["ultima_leitura"]
            ref_nivel = float(status_atual["nivel_atual"])
        else:
            ref = obter_ultima_leitura_ana(
                COD_TIMOTEO, apenas_com_nivel=True, client=db
            )
            if not ref:
                log.warning("Comparativo: sem dados recentes de Timóteo")
                return None
            ref_ts = ref["data_hora"]
            ref_nivel = float(ref["nivel_cm"])

        ref_dt = parse_iso_utc(ref_ts)
        if ref_dt is None:
            return None

        ref_ano = ref_dt.year
        log.info(
            "Comparativo: referência %02d/%02d %02dh · %.0f cm (%s)",
            ref_dt.day,
            ref_dt.month,
            ref_dt.hour,
            ref_nivel,
            ref_ano,
        )

        anos = list(range(2018, ref_ano))
        anos_anteriores: list[dict[str, Any]] = []

        with ThreadPoolExecutor(max_workers=4) as executor:
            resultados = executor.map(
                lambda ano: _buscar_nivel_ano_anterior(ano, ref_dt, db),
                anos,
            )
            for item in resultados:
                if item is not None:
                    anos_anteriores.append(item)

        anos_anteriores.sort(key=lambda x: x["ano"])

        if anos_anteriores:
            valores = [a["nivel_cm"] for a in anos_anteriores]
            estatisticas: dict[str, Optional[float]] = {
                "media": round(mean(valores), 1),
                "mediana": round(median(valores), 1),
                "min": round(min(valores), 1),
                "max": round(max(valores), 1),
            }
        else:
            estatisticas = {
                "media": None,
                "mediana": None,
                "min": None,
                "max": None,
            }

        return {
            "dia_mes": f"{ref_dt.day:02d}/{ref_dt.month:02d}",
            "hora": f"{ref_dt.hour:02d}:00",
            "atual": {
                "ano": ref_ano,
                "nivel_cm": round(ref_nivel, 1),
                "timestamp": ref_ts,
            },
            "anos_anteriores": anos_anteriores,
            "estatisticas": estatisticas,
        }
    except Exception as exc:
        log.error("Erro no comparativo anual: %s", exc)
        return None


def construir_defluencia_48h(
    client: Optional[Client] = None,
) -> Optional[dict[str, Any]]:
    """
    Consulta `historico_cemig` das últimas 48h e devolve série compacta para o sparkline.
    """
    db = client or obter_supabase()
    agora = datetime.now(timezone.utc)
    desde = agora - timedelta(hours=48)

    try:
        resp = (
            db.table("historico_cemig")
            .select("data_hora,defluencia_m3s")
            .not_.is_("defluencia_m3s", "null")
            .gte("data_hora", desde.isoformat())
            .lte("data_hora", agora.isoformat())
            .order("data_hora")
            .limit(500)
            .execute()
        )
    except Exception as exc:
        log.error("Erro ao consultar defluência 48h: %s", exc)
        return None

    if not resp.data:
        log.warning("Sem dados de defluência nas últimas 48h")
        return None

    pontos = []
    for item in resp.data:
        val = extrair_float(item.get("defluencia_m3s"))
        if val is not None:
            pontos.append({"t": item["data_hora"], "v": round(val, 2)})

    if not pontos:
        return None

    valores = [p["v"] for p in pontos]
    atual = valores[-1]
    primeiro = valores[0]
    variacao = ((atual - primeiro) / primeiro * 100.0) if primeiro > 0 else 0.0

    return {
        "inicio": pontos[0]["t"],
        "fim": pontos[-1]["t"],
        "pontos": pontos,
        "min": round(min(valores), 1),
        "max": round(max(valores), 1),
        "atual": round(atual, 1),
        "variacao_pct": round(variacao, 1),
    }


def gerar_historico_rio_7d(
    client: Optional[Client] = None,
    estacao: str = COD_TIMOTEO,
    nome: str = "MARIO DE CARVALHO",
) -> Optional[dict[str, Any]]:
    """
    Gera a chave `historico_rio_7d` do `cache_site` (7 dias agregados por hora máxima).
    Inclui tratamento de erros, filtragem de nulos e ordenação segura contra truncamento.
    """
    db = client or obter_supabase()
    limite = datetime.now(timezone.utc) - timedelta(days=7)

    try:
        resp = (
            db.table("historico_ana")
            .select("data_hora,nivel_cm")
            .eq("codigo_ana", estacao)
            .not_.is_("nivel_cm", "null")
            .gte("data_hora", limite.isoformat())
            .order("data_hora", desc=True)
            .limit(2000)
            .execute()
        )
    except Exception as exc:
        log.error("Erro ao consultar histórico 7d (%s): %s", estacao, exc)
        return None

    if not resp.data:
        return None

    buckets: dict[datetime, float] = {}
    for row in resp.data:
        dt = parse_iso_utc(row.get("data_hora", ""))
        nivel = extrair_float(row.get("nivel_cm"))
        if dt is None or nivel is None:
            continue
        chave = dt.replace(minute=0, second=0, microsecond=0)
        if chave not in buckets or nivel > buckets[chave]:
            buckets[chave] = nivel

    pontos = [
        {"t": k.isoformat().replace("+00:00", "Z"), "v": round(v, 1)}
        for k, v in sorted(buckets.items())
    ]
    if not pontos:
        return None

    valores = [p["v"] for p in pontos]
    nivel_atual = valores[-1]

    limite_24h = datetime.now(timezone.utc) - timedelta(hours=24)
    pontos_24h = [
        p["v"]
        for p in pontos
        if (dt_p := parse_iso_utc(p["t"])) is not None and dt_p >= limite_24h
    ]
    tendencia = (
        round(nivel_atual - pontos_24h[0], 1) if len(pontos_24h) >= 2 else None
    )

    return {
        "estacao": estacao,
        "nome": nome,
        "gerado_em": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "pontos": pontos,
        "min": round(min(valores), 1),
        "max": round(max(valores), 1),
        "tendencia_24h": tendencia,
        "referencias": {"atencao": 450, "alerta": 540, "inundacao": 620},
    }


def obter_status_camera() -> Optional[dict[str, Any]]:
    """Consulta a API do MediaMTX no VPS para saber se o stream está ativo."""
    sessao = obter_sessao_http()
    try:
        resp = sessao.get(VPS_CAMERA_URL, timeout=10)
        resp.raise_for_status()
        data = resp.json()

        ao_vivo = any(
            item.get("name") == "live/rio" and item.get("ready") is True
            for item in data.get("items", [])
        )
        return {
            "ao_vivo": ao_vivo,
            "verificado_em": datetime.now(timezone.utc).isoformat(),
        }
    except Exception as exc:
        log.error("Erro ao verificar status da câmera no VPS: %s", exc)
        return None

        
