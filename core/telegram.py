"""Cliente de notificações Telegram e lógica de transição de faixas/cooldown."""

import logging
import os
from datetime import datetime, timezone
from typing import Any, Callable, Optional

from core.ana_client import obter_sessao_http
from core.db import gravar_estado_alerta, obter_estado_alerta, parse_iso_utc

log = logging.getLogger(__name__)


def enviar_telegram(
    texto: str,
    token: Optional[str] = None,
    chat_id: Optional[str] = None,
    timeout: int = 30,
) -> bool:
    """Envia mensagem formatada em HTML para o chat do Telegram configurado."""
    tg_token = token or os.getenv("TELEGRAM_TOKEN")
    tg_chat = chat_id or os.getenv("TELEGRAM_CHAT_ID")

    if not tg_token or not tg_chat:
        log.error("TELEGRAM_TOKEN ou TELEGRAM_CHAT_ID não configurados.")
        return False

    sessao = obter_sessao_http()
    try:
        resp = sessao.post(
            f"https://api.telegram.org/bot{tg_token}/sendMessage",
            json={
                "chat_id": tg_chat,
                "text": texto,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            },
            timeout=timeout,
        )
        if resp.status_code != 200:
            log.error("Telegram HTTP %s: %s", resp.status_code, resp.text[:200])
            return False
        return True
    except Exception as exc:
        log.error("Erro ao enviar Telegram: %s", exc)
        return False


def cooldown_ativo(
    estado: Optional[dict[str, Any]],
    cooldown_min: float = 30.0,
) -> tuple[bool, float]:
    """
    Verifica se a chave está em período de cooldown.
    Retorna (esta_em_cooldown, minutos_restantes).
    """
    if not estado or not estado.get("notificado_em"):
        return False, 0.0

    notif_dt = parse_iso_utc(estado["notificado_em"])
    if notif_dt is None:
        return False, 0.0

    elapsed = (datetime.now(timezone.utc) - notif_dt).total_seconds() / 60.0
    if elapsed < cooldown_min:
        return True, cooldown_min - elapsed
    return False, 0.0


def obter_direcao_transicao(
    faixa_antiga: str,
    faixa_nova: str,
    ordem_faixas: list[str],
) -> str:
    """
    Determina o rótulo de direção da mudança de faixa ('ELEVAÇÃO', 'RECESSÃO'
    ou 'Estado atual') protegendo contra faixas antigas desconhecidas.
    """
    if faixa_antiga == "inicial" or faixa_antiga not in ordem_faixas:
        return "<b>Estado atual</b>"
    if faixa_nova not in ordem_faixas:
        return "<b>Estado atual</b>"

    idx_antigo = ordem_faixas.index(faixa_antiga)
    idx_novo = ordem_faixas.index(faixa_nova)
    return (
        "⬆️ <b>ELEVAÇÃO</b>"
        if idx_novo > idx_antigo
        else "⬇️ <b>RECESSÃO</b>"
    )


def processar_transicao_alerta(
    chave_estado: str,
    nome_exibicao: str,
    valor: float,
    unidade: str,
    formato_valor: str,
    faixa_nova: str,
    montar_mensagem_fn: Callable[[str, str], str],
    cooldown_min: float = 30.0,
    faixa_neutra: str = "normal",
) -> bool:
    """
    Executa a máquina de estados para alertas por faixa com anti-spam e cooldown.
    `montar_mensagem_fn(faixa_nova, faixa_antiga)` deve retornar o HTML da mensagem.
    Retorna True se uma notificação foi enviada ao Telegram.
    """
    estado = obter_estado_alerta(chave_estado)
    faixa_antiga = estado["ultima_faixa"] if estado else "inicial"

    valor_fmt = format(valor, formato_valor)
    log.info(
        "   %s: %s %s → %s (anterior: %s)",
        nome_exibicao,
        valor_fmt,
        unidade,
        faixa_nova,
        faixa_antiga,
    )

    # 1. Primeira leitura registrada no sistema
    if estado is None:
        notificar = faixa_nova != faixa_neutra
        enviou = False
        if notificar:
            msg = montar_mensagem_fn(faixa_nova, "inicial")
            enviou = enviar_telegram(msg)
            if enviou:
                log.info("   Primeira leitura notificada (%s)", faixa_nova)
        gravar_estado_alerta(chave_estado, faixa_nova, valor, enviou)
        return enviou

    # 2. Faixa inalterada
    if faixa_nova == faixa_antiga:
        gravar_estado_alerta(chave_estado, faixa_nova, valor, False)
        return False

    # 3. Verificação de cooldown
    em_cooldown, restante = cooldown_ativo(estado, cooldown_min)
    if em_cooldown:
        log.info(
            "   ⏸ Faixa mudou, mas cooldown ativo (%.0f min restantes)",
            restante,
        )
        gravar_estado_alerta(chave_estado, faixa_nova, valor, False)
        return False

    # 4. Notificação de mudança de faixa
    msg = montar_mensagem_fn(faixa_nova, faixa_antiga)
    ok = enviar_telegram(msg)
    gravar_estado_alerta(chave_estado, faixa_nova, valor, ok)
    if ok:
        log.info("   Notificação enviada: %s → %s", faixa_antiga, faixa_nova)
    return ok

