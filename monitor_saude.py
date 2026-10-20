# MONITORAMENTO DE SAÚDE — Alerta Enchente CDV
#
# Verifica se os dados estão chegando com regularidade e notifica no Telegram
# quando algum componente para de funcionar.
#
# Regra anti-spam: só notifica quando a FAIXA muda (OK → ATRASADO → PARADO).

import sys
from typing import Any, Callable, Optional

from core.config import (
    COD_NOVA_ERA,
    COD_TIMOTEO,
    TZ_BRASILIA,
    configurar_logger,
    validar_env,
)
from core.db import (
    calcular_idade_minutos,
    gravar_estado_alerta,
    obter_estado_alerta,
    obter_supabase,
    obter_ultima_leitura_ana,
    obter_ultima_leitura_cemig,
    parse_iso_utc,
)
from core.telegram import enviar_telegram

log = configurar_logger("monitor", formato_data="%H:%M:%S")


def _idade_de_timestamp(ts_iso: Optional[str]) -> tuple[Optional[float], Optional[str]]:
    if not ts_iso:
        return None, None
    idade = calcular_idade_minutos(ts_iso)
    return (round(idade, 1), ts_iso) if idade is not None else (None, None)


def _idade_de_ana(codigo: str) -> tuple[Optional[float], Optional[str]]:
    """Retorna (idade_min, ts_iso) da última leitura da estação ANA."""
    leitura = obter_ultima_leitura_ana(codigo, apenas_com_nivel=True)
    return _idade_de_timestamp(leitura["data_hora"]) if leitura else (None, None)


def _idade_de_cemig() -> tuple[Optional[float], Optional[str]]:
    """Retorna (idade_min, ts_iso) da última leitura da CEMIG."""
    leitura = obter_ultima_leitura_cemig()
    return _idade_de_timestamp(leitura["data_hora"]) if leitura else (None, None)


def _idade_de_cache() -> tuple[Optional[float], Optional[str]]:
    """Retorna (idade_min, ts_iso) da última atualização de `status_atual` em `cache_site`."""
    supabase = obter_supabase()
    try:
        resp = (
            supabase.table("cache_site")
            .select("atualizado_em")
            .eq("chave", "status_atual")
            .limit(1)
            .execute()
        )
        if resp.data:
            return _idade_de_timestamp(resp.data[0]["atualizado_em"])
    except Exception as exc:
        log.error("Erro ao ler cache_site: %s", exc)
    return None, None


def _idade_da_camera() -> tuple[Optional[float], Optional[str]]:
    """
    Retorna (idade_ficticia, ts_iso) para a câmera ao vivo:
      - Câmera online  → (0, verificado_em)    → faixa 'ok'
      - Câmera offline → (9999, verificado_em) → faixa 'parado'
    """
    supabase = obter_supabase()
    try:
        resp = (
            supabase.table("cache_site")
            .select("dados,atualizado_em")
            .eq("chave", "camera_status")
            .limit(1)
            .execute()
        )
    except Exception as exc:
        log.error("Erro ao ler camera_status: %s", exc)
        return None, None

    if not resp.data:
        log.warning("camera_status ausente no cache")
        return None, None

    row = resp.data[0]
    dados = row.get("dados") or {}
    ao_vivo = bool(dados.get("ao_vivo", False))
    verificado_em = dados.get("verificado_em") or row.get("atualizado_em")

    return (0.0 if ao_vivo else 9999.0), verificado_em


CHECKS: list[tuple[str, str, Callable[..., tuple[Optional[float], Optional[str]]], tuple[Any, ...], float]] = [
    ("saude_ana_novaera", "ANA · Nova Era", _idade_de_ana, (COD_NOVA_ERA,), 120.0),
    ("saude_ana_timoteo", "ANA · Timóteo", _idade_de_ana, (COD_TIMOTEO,), 120.0),
    ("saude_cemig", "CEMIG · Sá Carvalho", _idade_de_cemig, (), 180.0),
    ("saude_cache", "Cache do site", _idade_de_cache, (), 120.0),
    ("saude_camera", "Câmera ao vivo", _idade_da_camera, (), 30.0),
]


def classificar(idade_min: Optional[float], limite_min: float) -> str:
    """
    Classifica a idade do dado:
      - ok      : dentro do limite
      - atrasado: até 2x o limite
      - parado  : mais que 2x o limite
      - sem_dado: nunca chegou
    """
    if idade_min is None:
        return "sem_dado"
    if idade_min <= limite_min:
        return "ok"
    if idade_min <= limite_min * 2:
        return "atrasado"
    return "parado"


def formatar_idade(minutos: Optional[float]) -> str:
    """Formata minutos decorridos em texto amigável."""
    if minutos is None:
        return "sem dado"
    if minutos < 60:
        return f"há {int(minutos)} min"
    horas = minutos / 60.0
    if horas < 24:
        return f"há {horas:.1f}h"
    dias = horas / 24.0
    return f"há {dias:.1f} dias"


def montar_alerta(
    nome: str,
    nova_faixa: str,
    idade_min: Optional[float],
    ts_iso: Optional[str],
    limite_min: float,
) -> str:
    """Monta mensagem HTML de alerta de saúde do sistema."""
    mapa_status = {
        "parado": ("🔴", "PARADO"),
        "atrasado": ("🟡", "ATRASADO"),
        "sem_dado": ("🔴", "SEM DADOS"),
    }
    emoji, titulo = mapa_status.get(nova_faixa, ("🟢", "NORMALIZADO"))

    linhas = [
        f"{emoji} <b>MONITORAMENTO — {titulo}</b>",
        "",
        f"📌 <b>{nome}</b>",
    ]

    dt = parse_iso_utc(ts_iso or "")
    if dt is not None:
        dt_brt = dt.astimezone(TZ_BRASILIA).strftime("%d/%m %H:%M")
        linhas.append(f"Última leitura: {dt_brt} ({formatar_idade(idade_min)})")
    else:
        linhas.append("Nunca chegou dado nesta tabela")

    if nova_faixa != "ok":
        linhas += [
            "",
            f"Limite esperado: {int(limite_min / 60)}h",
            "Verifique: https://github.com/ryandevbr/Alerta_Enchente_CDV/actions",
        ]

    return "\n".join(linhas)


def processar_check(
    chave: str,
    nome: str,
    funcao: Callable[..., tuple[Optional[float], Optional[str]]],
    args: tuple[Any, ...],
    limite_min: float,
) -> bool:
    """Executa um check de saúde e notifica no Telegram caso a faixa mude."""
    idade_min, ts_iso = funcao(*args)
    faixa_nova = classificar(idade_min, limite_min)
    valor_estado = 0.0 if idade_min is None else idade_min

    estado = obter_estado_alerta(chave)
    faixa_antiga = estado["ultima_faixa"] if estado else "inicial"

    log.info(
        "   %s: %s → %s (anterior: %s)",
        nome,
        formatar_idade(idade_min),
        faixa_nova,
        faixa_antiga,
    )

    if faixa_nova == faixa_antiga:
        gravar_estado_alerta(chave, faixa_nova, valor_estado, False)
        return False

    if estado is None:
        notificar = faixa_nova in ("atrasado", "parado", "sem_dado")
        enviou = False
        if notificar:
            msg = montar_alerta(nome, faixa_nova, idade_min, ts_iso, limite_min)
            enviou = enviar_telegram(msg)
            if enviou:
                log.info("   Alerta inicial enviado (%s)", faixa_nova)
        gravar_estado_alerta(chave, faixa_nova, valor_estado, enviou)
        return enviou

    msg = montar_alerta(nome, faixa_nova, idade_min, ts_iso, limite_min)
    ok = enviar_telegram(msg)
    gravar_estado_alerta(chave, faixa_nova, valor_estado, ok)
    if ok:
        if faixa_nova == "ok":
            log.info("   Notificação de normalização enviada")
        else:
            log.info("   Alerta enviado: %s → %s", faixa_antiga, faixa_nova)
    return ok


def main() -> int:
    validar_env(
        "SUPABASE_URL", "SUPABASE_KEY", "TELEGRAM_TOKEN", "TELEGRAM_CHAT_ID"
    )
    obter_supabase()

    log.info("=" * 60)
    log.info("MONITORAMENTO DE SAÚDE")
    log.info("=" * 60)

    alertas = 0
    for chave, nome, funcao, args, limite in CHECKS:
        try:
            if processar_check(chave, nome, funcao, args, limite):
                alertas += 1
        except Exception as exc:
            log.exception("Erro no check %s: %s", nome, exc)

    log.info("Fim — %s notificação(ões) enviada(s)", alertas)
    return 0


if __name__ == "__main__":
    sys.exit(main())
