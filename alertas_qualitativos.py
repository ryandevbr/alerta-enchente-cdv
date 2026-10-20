# ALERTAS QUALITATIVOS POR FAIXA DE COTA
# Alerta Enchente CDV
#
# Monitora dois tipos de fontes:
#   1. ANA (Nova Era + Timóteo) → alerta por nível absoluto
#   2. CEMIG (UHE Sá Carvalho) → alerta por defluência
#
# Regra anti-spam: só notifica quando a FAIXA muda (não o valor).
# Cooldown: 30 min mínimo entre notificações da mesma estação.

import sys
from datetime import timedelta

from core.config import (
    ESTACOES_ANA_ALERTA,
    TZ_BRASILIA,
    configurar_logger,
    validar_env,
)
from core.db import (
    calcular_idade_minutos,
    obter_supabase,
    obter_ultima_leitura_ana,
    obter_ultima_leitura_cemig,
    parse_iso_utc,
)
from core.telegram import obter_direcao_transicao, processar_transicao_alerta

log = configurar_logger("alertas", formato_data="%H:%M:%S")

# FAIXAS DE NÍVEL (ANA) — em cm
FAIXAS_ANA = {
    "normal": {"min": -9999.0, "max": 779.99, "emoji": "🟢", "label": "NORMAL"},
    "alerta": {"min": 780.0, "max": 889.99, "emoji": "🟡", "label": "ALERTA"},
    "critico": {"min": 890.0, "max": 99999.0, "emoji": "🔴", "label": "CRÍTICO"},
}
ORDEM_FAIXAS_ANA = ["normal", "alerta", "critico"]

# FAIXAS DE DEFLUÊNCIA (CEMIG) — em m³/s
# Baseline histórica: ~17 m³/s | Gatilho oficial CEMIG (Qr): 550 m³/s
FAIXAS_DEFLUENCIA = [
    (550.0, "critico", "🔴", "CRÍTICO · vazão de restrição atingida"),
    (100.0, "alerta", "🟠", "ALERTA · vazão elevada"),
    (30.0, "atencao", "🟡", "ATENÇÃO · vazão acima do normal"),
]
ORDEM_DEFLUENCIA = ["normal", "atencao", "alerta", "critico"]

# Parâmetros de comportamento
COOLDOWN_MIN = 30.0
IDADE_MAXIMA_DADO_MIN = 180.0
IDADE_MAXIMA_CEMIG_MIN = 180.0


def classificar_faixa_ana(nivel: float) -> str:
    """Classifica o nível do rio (cm) na faixa correspondente da ANA."""
    for nome, faixa in FAIXAS_ANA.items():
        if faixa["min"] <= nivel <= faixa["max"]:
            return nome
    return "normal"


def montar_mensagem_ana(
    nome: str,
    faixa_nova: str,
    faixa_antiga: str,
    nivel: float,
    ts_iso: str,
    lag: int,
) -> str:
    """Monta mensagem HTML de alerta para estações fluviométricas da ANA."""
    f = FAIXAS_ANA[faixa_nova]
    dt = parse_iso_utc(ts_iso)
    dt_brt = dt.astimezone(TZ_BRASILIA).strftime("%d/%m %H:%M") if dt else ts_iso
    direcao = obter_direcao_transicao(
        faixa_antiga, faixa_nova, ORDEM_FAIXAS_ANA
    )

    linhas = [
        f"{f['emoji']} <b>{f['label']} — {nome}</b>",
        f"{direcao}",
        "",
        f"Nível: <b>{nivel:.0f} cm</b>",
        f"Leitura: {dt_brt} (BRT)",
    ]

    if lag > 0 and faixa_nova != "normal" and dt is not None:
        previsao = (dt + timedelta(hours=lag)).astimezone(TZ_BRASILIA)
        linhas += [
            "",
            "<b>Onda de cheia a caminho de Timóteo.</b>",
            f"Chegada prevista: ~{lag}h ({previsao.strftime('%d/%m %H:%M')} BRT)",
        ]
    elif lag == 0 and faixa_nova == "critico":
        linhas += ["", "<b>Cheia em curso no bairro. Ação imediata.</b>"]
    elif lag == 0 and faixa_nova == "normal":
        linhas += ["", "Nível retornou à faixa segura."]

    return "\n".join(linhas)


def processar_estacao(codigo: str, nome: str, lag: int) -> bool:
    """Verifica a última leitura de uma estação da ANA e dispara alerta se mudou de faixa."""
    leitura = obter_ultima_leitura_ana(codigo, apenas_com_nivel=True)
    if not leitura or leitura.get("nivel_cm") is None:
        log.warning("   %s: sem dados recentes", nome)
        return False

    nivel = float(leitura["nivel_cm"])
    ts = leitura["data_hora"]
    idade_min = calcular_idade_minutos(ts)

    if idade_min is None or idade_min > IDADE_MAXIMA_DADO_MIN:
        log.warning(
            "   %s: dado com %.0f min — pulando", nome, idade_min or -1
        )
        return False

    faixa_nova = classificar_faixa_ana(nivel)
    return processar_transicao_alerta(
        chave_estado=codigo,
        nome_exibicao=nome,
        valor=nivel,
        unidade="cm",
        formato_valor=".0f",
        faixa_nova=faixa_nova,
        montar_mensagem_fn=lambda f_nova, f_ant: montar_mensagem_ana(
            nome, f_nova, f_ant, nivel, ts, lag
        ),
        cooldown_min=COOLDOWN_MIN,
    )


def classificar_defluencia(vazao: float) -> str:
    """Classifica a defluência da UHE Sá Carvalho (m³/s)."""
    for limiar, nome, _, _ in FAIXAS_DEFLUENCIA:
        if vazao >= limiar:
            return nome
    return "normal"


def _metadados_defluencia(faixa: str) -> tuple[str, str]:
    """Retorna (emoji, label) para uma faixa de defluência da CEMIG."""
    for _, nome, emoji, label in FAIXAS_DEFLUENCIA:
        if nome == faixa:
            return emoji, label
    return "🟢", "NORMAL"


def montar_mensagem_cemig(
    faixa_nova: str,
    faixa_antiga: str,
    vazao: float,
    ts_iso: str,
) -> str:
    """Monta mensagem HTML de alerta para defluência da UHE Sá Carvalho (CEMIG)."""
    emoji, label = _metadados_defluencia(faixa_nova)
    dt = parse_iso_utc(ts_iso)
    dt_brt = dt.astimezone(TZ_BRASILIA).strftime("%d/%m %H:%M") if dt else ts_iso
    direcao = obter_direcao_transicao(
        faixa_antiga, faixa_nova, ORDEM_DEFLUENCIA
    )

    linhas = [
        f"{emoji} <b>DEFLUÊNCIA — UHE Sá Carvalho</b>",
        f"<b>{label}</b>",
        f"{direcao}",
        "",
        f"Defluência: <b>{vazao:.0f} m³/s</b>",
        f"Leitura: {dt_brt} (BRT)",
    ]

    if faixa_nova in ("atencao", "alerta", "critico") and dt is not None:
        previsao = (dt + timedelta(hours=4)).astimezone(TZ_BRASILIA)
        linhas += [
            "",
            "<b>CEMIG liberando água da barragem.</b>",
            f"Chegada em Timóteo: ~4h ({previsao.strftime('%d/%m %H:%M')} BRT)",
        ]
    elif faixa_nova == "normal" and faixa_antiga != "inicial":
        linhas += ["", "Defluência retornou ao nível normal."]

    return "\n".join(linhas)


def processar_cemig() -> bool:
    """Verifica a última defluência da UHE Sá Carvalho e dispara alerta se mudou de faixa."""
    chave = "CEMIG_SA_CARVALHO"
    leitura = obter_ultima_leitura_cemig(apenas_com_defluencia=True)
    if not leitura or leitura.get("defluencia_m3s") is None:
        log.warning("   Sá Carvalho: sem dados de defluência")
        return False

    vazao = float(leitura["defluencia_m3s"])
    ts = leitura["data_hora"]
    idade_min = calcular_idade_minutos(ts)

    if idade_min is None or idade_min > IDADE_MAXIMA_CEMIG_MIN:
        log.warning(
            "   Sá Carvalho: dado com %.0f min — pulando", idade_min or -1
        )
        return False

    faixa_nova = classificar_defluencia(vazao)
    return processar_transicao_alerta(
        chave_estado=chave,
        nome_exibicao="Sá Carvalho",
        valor=vazao,
        unidade="m³/s",
        formato_valor=".1f",
        faixa_nova=faixa_nova,
        montar_mensagem_fn=lambda f_nova, f_ant: montar_mensagem_cemig(
            f_nova, f_ant, vazao, ts
        ),
        cooldown_min=COOLDOWN_MIN,
    )


def main() -> int:
    validar_env(
        "SUPABASE_URL", "SUPABASE_KEY", "TELEGRAM_TOKEN", "TELEGRAM_CHAT_ID"
    )
    obter_supabase()

    log.info("=" * 60)
    log.info("VERIFICAÇÃO DE ALERTAS POR FAIXA")
    log.info("=" * 60)

    notificacoes = 0

    for codigo, nome, lag in ESTACOES_ANA_ALERTA:
        try:
            if processar_estacao(codigo, nome, lag):
                notificacoes += 1
        except Exception as exc:
            log.exception("Erro processando %s: %s", nome, exc)

    try:
        if processar_cemig():
            notificacoes += 1
    except Exception as exc:
        log.exception("Erro processando CEMIG: %s", exc)

    log.info("Fim — %s notificação(ões) enviada(s)", notificacoes)
    return 0


if __name__ == "__main__":
    sys.exit(main())
