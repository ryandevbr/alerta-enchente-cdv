"""Cálculos hidrológicos preditivos (velocidade suavizada e ocupação de calha)."""

from datetime import datetime
import logging
from typing import Any

LIMIAR_SALTO_RUIDO_CM = 50.0
DELTA_T_MINIMO_HORAS = 0.16  # ~10 minutos


# 1. MOTOR PREDITIVO (Velocidade e Tendência)

def calcular_velocidade_suavizada(
    leituras_supabase: list[dict[str, Any]],
) -> dict[str, Any]:
    """
    Recebe as últimas 4 a 5 leituras (aprox. 1 hora) do Supabase,
    ordenadas da mais recente (`[0]`) para a mais antiga (`[-1]`).
    Aplica filtro de ruído (rejeição de outliers) e calcula a derivada temporal.
    """
    resultado_padrao = {
        "velocidade": 0.0,
        "tendencia": "ESTÁVEL",
        "confiavel": False,
    }
    if len(leituras_supabase) < 2:
        return resultado_padrao

    # 1. Filtro Anti-Ruído (Outlier Rejection)
    # O rio não sobe/desce mais de 50 cm em apenas 15 minutos de forma natural.
    # Se isso ocorrer, é detrito batendo no sensor da ANA. Descartamos o salto.
    leituras_validas: list[dict[str, Any]] = [leituras_supabase[0]]
    for leitura in leituras_supabase[1:]:
        nivel_ref = leituras_validas[-1].get("nivel_cm")
        nivel_cand = leitura.get("nivel_cm")
        if nivel_ref is None or nivel_cand is None:
            continue

        if abs(float(nivel_ref) - float(nivel_cand)) > LIMIAR_SALTO_RUIDO_CM:
            logging.warning(
                "Ruído de sensor ignorado: salto de %s para %s",
                nivel_cand,
                nivel_ref,
            )
            continue

        leituras_validas.append(leitura)

    if len(leituras_validas) < 2:
        return resultado_padrao

    # 2. Cálculo da Derivada Suavizada (Delta N / Delta t)
    recente = leituras_validas[0]
    antiga = leituras_validas[-1]

    try:
        t1 = datetime.fromisoformat(recente["data_hora"].replace("Z", "+00:00"))
        t0 = datetime.fromisoformat(antiga["data_hora"].replace("Z", "+00:00"))
    except (KeyError, ValueError, AttributeError):
        return resultado_padrao

    delta_t_horas = (t1 - t0).total_seconds() / 3600.0
    if delta_t_horas <= DELTA_T_MINIMO_HORAS:
        return resultado_padrao

    delta_n_cm = float(recente["nivel_cm"]) - float(antiga["nivel_cm"])
    velocidade_cm_h = delta_n_cm / delta_t_horas

    # 3. Classificação Lexical Segura
    tendencia = "ESTÁVEL"
    if velocidade_cm_h > 2.0:
        tendencia = "SUBINDO"
    elif velocidade_cm_h < -2.0:
        tendencia = "BAIXANDO"

    return {
        "velocidade": round(velocidade_cm_h, 1),
        "tendencia": tendencia,
        "confiavel": True,
    }


# 2. MOTOR DE IMPACTO HIPERLOCAL (Ocupação)

def calcular_ocupacao_calha(
    nivel_atual: float,
    ruas_banco: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """
    Traduz a cota hidrológica para o impacto percentual em cada rua cadastrada.
    """
    relatorio_ruas: list[dict[str, Any]] = []

    for rua in ruas_banco:
        cota_critica = rua.get("nivel_critico_cm")
        if cota_critica is None or float(cota_critica) <= 0:
            logging.warning(
                "Rua '%s' com nivel_critico_cm inválido (%s) — ignorada.",
                rua.get("nome"),
                cota_critica,
            )
            continue

        ocupacao_pct = (float(nivel_atual) / float(cota_critica)) * 100.0

        relatorio_ruas.append(
            {
                "nome": rua.get("nome", ""),
                "bairro": rua.get("bairro", ""),
                "ocupacao_pct": round(ocupacao_pct, 1),
                "critico": ocupacao_pct >= 100.0,
                "alerta": 80.0 <= ocupacao_pct < 100.0,
            }
        )

    # Retorna a lista ordenada: as ruas em maior perigo aparecem primeiro
    return sorted(relatorio_ruas, key=lambda x: x["ocupacao_pct"], reverse=True)
