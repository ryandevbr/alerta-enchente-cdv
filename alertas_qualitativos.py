# ALERTAS QUALITATIVOS POR FAIXA DE COTA
# Alerta Enchente CDV
#
# Monitora dois tipos de fontes:
#   1. ANA (Nova Era + Timóteo) → alerta por nível absoluto
#   2. CEMIG (UHE Sá Carvalho) → alerta por defluência
#
# Regra anti-spam: só notifica quando a FAIXA muda (não o valor).
# Cooldown: 30 min mínimo entre notificações da mesma estação.


import os
import sys
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

import requests
from dotenv import load_dotenv
from supabase import create_client, Client

# CONFIG

load_dotenv()

SUPABASE_URL   = os.getenv("SUPABASE_URL")
SUPABASE_KEY   = os.getenv("SUPABASE_KEY")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT  = os.getenv("TELEGRAM_CHAT_ID")

if not all([SUPABASE_URL, SUPABASE_KEY, TELEGRAM_TOKEN, TELEGRAM_CHAT]):
    print("Faltam variáveis no .env")
    sys.exit(1)

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(message)s",
                    datefmt="%H:%M:%S")
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
log = logging.getLogger("alertas")

TZ_BRASILIA = timezone(timedelta(hours=-3))

# FAIXAS DE NÍVEL (ANA) — em cm

FAIXAS = {
    "normal":  {"min": -9999, "max": 779.99, "emoji": "🟢", "label": "NORMAL"},
    "alerta":  {"min": 780,   "max": 889.99, "emoji": "🟡", "label": "ALERTA"},
    "critico": {"min": 890,   "max": 99999,  "emoji": "🔴", "label": "CRÍTICO"},
}
ORDEM_FAIXAS = ["normal", "alerta", "critico"]

# Estações monitoradas: (código, nome amigável, lag até Timóteo em horas)
ESTACOES = [
    ("56661000", "Nova Era", 9),
    ("56696000", "Timóteo",  0),
]

# FAIXAS DE DEFLUÊNCIA (CEMIG) — em m³/s

# Baseline histórica: ~17 m³/s
# Gatilho oficial CEMIG (Qr): 550 m³/s
FAIXAS_DEFLUENCIA = [
    (550, "critico", "🔴", "CRÍTICO · vazão de restrição atingida"),
    (100, "alerta",  "🟠", "ALERTA · vazão elevada"),
    (30,  "atencao", "🟡", "ATENÇÃO · vazão acima do normal"),
]
ORDEM_DEFLUENCIA = ["normal", "atencao", "alerta", "critico"]

# Parâmetros de comportamento

COOLDOWN_MIN = 30                 # min entre notificações da mesma estação
IDADE_MAXIMA_DADO_MIN = 180       # min — ANA
IDADE_MAXIMA_CEMIG_MIN = 180      # min — CEMIG

# HELPERS GENÉRICOS

def enviar_telegram(texto: str) -> bool:
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={
                "chat_id": TELEGRAM_CHAT,
                "text": texto,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            },
            timeout=30,
        )
        if r.status_code != 200:
            log.error(f"Telegram HTTP {r.status_code}: {r.text[:200]}")
            return False
        return True
    except Exception as e:
        log.error(f"Erro ao enviar Telegram: {e}")
        return False


def obter_estado(chave: str):
    try:
        r = (supabase.table("alertas_estado")
             .select("*").eq("estacao", chave).execute())
        if r.data:
            return r.data[0]
    except Exception:
        pass
    return None


def gravar_estado(chave: str, faixa: str, valor: float, notificou: bool):
    agora = datetime.now(timezone.utc).isoformat()
    payload = {
        "estacao":        chave,
        "ultima_faixa":   faixa,
        "ultimo_nivel":   valor,
        "ultima_mudanca": agora,
    }
    if notificou:
        payload["notificado_em"] = agora
    try:
        supabase.table("alertas_estado").upsert(
            payload, on_conflict="estacao"
        ).execute()
    except Exception as e:
        log.error(f"Erro ao gravar estado: {e}")


def cooldown_ativo(estado: dict) -> tuple[bool, float]:
    """Retorna (esta_em_cooldown, minutos_restantes)."""
    if not estado or not estado.get("notificado_em"):
        return False, 0
    notif_dt = datetime.fromisoformat(
        estado["notificado_em"].replace("Z", "+00:00")
    )
    elapsed = (datetime.now(timezone.utc) - notif_dt).total_seconds() / 60
    if elapsed < COOLDOWN_MIN:
        return True, COOLDOWN_MIN - elapsed
    return False, 0

# PROCESSAMENTO — ESTAÇÕES ANA (nível)

def classificar_faixa(nivel: float) -> str:
    for nome, faixa in FAIXAS.items():
        if faixa["min"] <= nivel <= faixa["max"]:
            return nome
    return "normal"


def obter_ultimo_nivel(codigo: str):
    try:
        r = (supabase.table("historico_ana")
             .select("data_hora,nivel_cm")
             .eq("codigo_ana", codigo)
             .not_.is_("nivel_cm", "null")
             .order("data_hora", desc=True)
             .limit(1)
             .execute())
        if r.data:
            return float(r.data[0]["nivel_cm"]), r.data[0]["data_hora"]
    except Exception as e:
        log.error(f"Erro ao ler {codigo}: {e}")
    return None, None


def montar_mensagem_ana(nome: str, faixa_nova: str, faixa_antiga: str,
                        nivel: float, ts_iso: str, lag: int) -> str:
    f = FAIXAS[faixa_nova]
    dt = datetime.fromisoformat(ts_iso.replace("Z", "+00:00"))
    dt_brt = dt.astimezone(TZ_BRASILIA).strftime("%d/%m %H:%M")

    if faixa_antiga == "inicial":
        direcao = "<b>Estado atual</b>"
    else:
        idx_antigo = ORDEM_FAIXAS.index(faixa_antiga)
        idx_novo   = ORDEM_FAIXAS.index(faixa_nova)
        direcao = "⬆️ <b>ELEVAÇÃO</b>" if idx_novo > idx_antigo else "⬇️ <b>RECESSÃO</b>"

    linhas = [
        f"{f['emoji']} <b>{f['label']} — {nome}</b>",
        f"{direcao}",
        "",
        f"Nível: <b>{nivel:.0f} cm</b>",
        f"Leitura: {dt_brt} (BRT)",
    ]

    if lag > 0 and faixa_nova != "normal":
        previsao = (dt + timedelta(hours=lag)).astimezone(TZ_BRASILIA)
        linhas += [
            "",
            f"<b>Onda de cheia a caminho de Timóteo.</b>",
            f"Chegada prevista: ~{lag}h "
            f"({previsao.strftime('%d/%m %H:%M')} BRT)",
        ]
    elif lag == 0 and faixa_nova == "critico":
        linhas += ["", "<b>Cheia em curso no bairro. Ação imediata.</b>"]
    elif lag == 0 and faixa_nova == "normal":
        linhas += ["", "Nível retornou à faixa segura."]

    return "\n".join(linhas)


def processar_estacao(codigo: str, nome: str, lag: int) -> bool:
    nivel, ts = obter_ultimo_nivel(codigo)
    if nivel is None or ts is None:
        log.warning(f"   {nome}: sem dados recentes")
        return False

    dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    idade_min = (datetime.now(timezone.utc) - dt).total_seconds() / 60
    if idade_min > IDADE_MAXIMA_DADO_MIN:
        log.warning(f"   {nome}: dado com {idade_min:.0f} min — pulando")
        return False

    faixa_nova = classificar_faixa(nivel)
    estado = obter_estado(codigo)
    faixa_antiga = estado["ultima_faixa"] if estado else "inicial"

    log.info(f"   {nome}: {nivel:.0f} cm → {faixa_nova} "
             f"(anterior: {faixa_antiga})")

    # Primeira vez no sistema
    if estado is None:
        notificar = faixa_nova != "normal"
        if notificar:
            msg = montar_mensagem_ana(nome, faixa_nova, "inicial", nivel, ts, lag)
            if enviar_telegram(msg):
                log.info(f"   Primeira leitura notificada ({faixa_nova})")
        gravar_estado(codigo, faixa_nova, nivel, notificar)
        return notificar

    # Faixa não mudou
    if faixa_nova == faixa_antiga:
        gravar_estado(codigo, faixa_nova, nivel, False)
        return False

    # Cooldown
    em_cooldown, restante = cooldown_ativo(estado)
    if em_cooldown:
        log.info(f"   ⏸ Faixa mudou, mas cooldown ativo "
                 f"({restante:.0f} min restantes)")
        gravar_estado(codigo, faixa_nova, nivel, False)
        return False

    # Notifica
    msg = montar_mensagem_ana(nome, faixa_nova, faixa_antiga, nivel, ts, lag)
    ok = enviar_telegram(msg)
    gravar_estado(codigo, faixa_nova, nivel, ok)
    if ok:
        log.info(f"   Notificação enviada: {faixa_antiga} → {faixa_nova}")
    return ok


# PROCESSAMENTO — CEMIG (defluência)

def classificar_defluencia(vazao: float) -> str:
    for limiar, nome, _, _ in FAIXAS_DEFLUENCIA:
        if vazao >= limiar:
            return nome
    return "normal"


def obter_ultima_defluencia_cemig():
    try:
        r = (supabase.table("historico_cemig")
             .select("data_hora,defluencia_m3s")
             .not_.is_("defluencia_m3s", "null")
             .order("data_hora", desc=True)
             .limit(1)
             .execute())
        if r.data:
            return float(r.data[0]["defluencia_m3s"]), r.data[0]["data_hora"]
    except Exception as e:
        log.error(f"Erro ao ler historico_cemig: {e}")
    return None, None


def emoji_defluencia(faixa: str) -> str:
    for _, nome, emoji, _ in FAIXAS_DEFLUENCIA:
        if nome == faixa:
            return emoji
    return "🟢"


def label_defluencia(faixa: str) -> str:
    if faixa == "normal":
        return "NORMAL"
    for _, nome, _, label in FAIXAS_DEFLUENCIA:
        if nome == faixa:
            return label
    return "NORMAL"


def montar_mensagem_cemig(faixa_nova: str, faixa_antiga: str,
                          vazao: float, ts_iso: str) -> str:
    emoji = emoji_defluencia(faixa_nova)
    label = label_defluencia(faixa_nova)
    dt = datetime.fromisoformat(ts_iso.replace("Z", "+00:00"))
    dt_brt = dt.astimezone(TZ_BRASILIA).strftime("%d/%m %H:%M")

    if faixa_antiga == "inicial":
        direcao = "<b>Estado atual</b>"
    else:
        idx_antigo = ORDEM_DEFLUENCIA.index(faixa_antiga)
        idx_novo   = ORDEM_DEFLUENCIA.index(faixa_nova)
        direcao = ("⬆️ <b>ELEVAÇÃO</b>" if idx_novo > idx_antigo
                   else "⬇️ <b>RECESSÃO</b>")

    linhas = [
        f"{emoji} <b>DEFLUÊNCIA — UHE Sá Carvalho</b>",
        f"<b>{label}</b>",
        f"{direcao}",
        "",
        f"Defluência: <b>{vazao:.0f} m³/s</b>",
        f"Leitura: {dt_brt} (BRT)",
    ]

    if faixa_nova in ("atencao", "alerta", "critico"):
        previsao = (dt + timedelta(hours=4)).astimezone(TZ_BRASILIA)
        linhas += [
            "",
            "<b>CEMIG liberando água da barragem.</b>",
            f"Chegada em Timóteo: ~4h "
            f"({previsao.strftime('%d/%m %H:%M')} BRT)",
        ]
    elif faixa_nova == "normal" and faixa_antiga != "inicial":
        linhas += ["", "Defluência retornou ao nível normal."]

    return "\n".join(linhas)


def processar_cemig() -> bool:
    CHAVE = "CEMIG_SA_CARVALHO"

    vazao, ts = obter_ultima_defluencia_cemig()
    if vazao is None or ts is None:
        log.warning("   Sá Carvalho: sem dados de defluência")
        return False

    dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    idade_min = (datetime.now(timezone.utc) - dt).total_seconds() / 60
    if idade_min > IDADE_MAXIMA_CEMIG_MIN:
        log.warning(f"   Sá Carvalho: dado com {idade_min:.0f} min — pulando")
        return False

    faixa_nova = classificar_defluencia(vazao)
    estado = obter_estado(CHAVE)
    faixa_antiga = estado["ultima_faixa"] if estado else "inicial"

    log.info(f"   Sá Carvalho: {vazao:.1f} m³/s → {faixa_nova} "
             f"(anterior: {faixa_antiga})")

    # Primeira vez no sistema
    if estado is None:
        notificar = faixa_nova != "normal"
        if notificar:
            msg = montar_mensagem_cemig(faixa_nova, "inicial", vazao, ts)
            if enviar_telegram(msg):
                log.info(f"   Primeira leitura notificada ({faixa_nova})")
        gravar_estado(CHAVE, faixa_nova, vazao, notificar)
        return notificar

    # Faixa não mudou
    if faixa_nova == faixa_antiga:
        gravar_estado(CHAVE, faixa_nova, vazao, False)
        return False

    # Cooldown
    em_cooldown, restante = cooldown_ativo(estado)
    if em_cooldown:
        log.info(f"   ⏸ Faixa mudou, mas cooldown ativo "
                 f"({restante:.0f} min restantes)")
        gravar_estado(CHAVE, faixa_nova, vazao, False)
        return False

    # Notifica
    msg = montar_mensagem_cemig(faixa_nova, faixa_antiga, vazao, ts)
    ok = enviar_telegram(msg)
    gravar_estado(CHAVE, faixa_nova, vazao, ok)
    if ok:
        log.info(f"   Notificação enviada: {faixa_antiga} → {faixa_nova}")
    return ok

# MAIN

def main() -> int:
    log.info("=" * 60)
    log.info("VERIFICAÇÃO DE ALERTAS POR FAIXA")
    log.info("=" * 60)

    notificacoes = 0

    # ANA (níveis)
    for codigo, nome, lag in ESTACOES:
        try:
            if processar_estacao(codigo, nome, lag):
                notificacoes += 1
        except Exception as e:
            log.exception(f"Erro processando {nome}: {e}")

    # CEMIG (defluência)
    try:
        if processar_cemig():
            notificacoes += 1
    except Exception as e:
        log.exception(f"Erro processando CEMIG: {e}")

    log.info(f"Fim — {notificacoes} notificação(ões) enviada(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
