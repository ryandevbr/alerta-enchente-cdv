
# MONITORAMENTO DE SAÚDE — Alerta Enchente CDV
#
# Verifica se os dados estão chegando com regularidade e notifica no Telegram
# quando algum componente para de funcionar.
#
# Regra anti-spam: só notifica quando a FAIXA muda (OK → ATRASADO → PARADO).


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

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
log = logging.getLogger("monitor")

TZ_BRASILIA = timezone(timedelta(hours=-3))


# DEFINIÇÃO DOS CHECKS

# Cada check: (nome_chave, nome_amigavel, funcao_idade_min, limite_min)
# A funcao_idade_min deve retornar (idade_min, timestamp_iso) ou (None, None)

COD_NE = "56661000"
COD_T  = "56696000"


def _idade_de_ana(codigo: str):
    """Retorna idade (min) da última leitura da estação ANA."""
    try:
        r = (supabase.table("historico_ana")
             .select("data_hora")
             .eq("codigo_ana", codigo)
             .not_.is_("nivel_cm", "null")
             .order("data_hora", desc=True)
             .limit(1)
             .execute())
        if r.data:
            return _idade_desde(r.data[0]["data_hora"])
    except Exception as e:
        log.error(f"Erro ao ler ANA {codigo}: {e}")
    return None, None


def _idade_de_cemig():
    try:
        r = (supabase.table("historico_cemig")
             .select("data_hora")
             .order("data_hora", desc=True)
             .limit(1)
             .execute())
        if r.data:
            return _idade_desde(r.data[0]["data_hora"])
    except Exception as e:
        log.error(f"Erro ao ler historico_cemig: {e}")
    return None, None


def _idade_de_cache():
    try:
        r = (supabase.table("cache_site")
             .select("atualizado_em")
             .eq("chave", "status_atual")
             .limit(1)
             .execute())
        if r.data:
            return _idade_desde(r.data[0]["atualizado_em"])
    except Exception as e:
        log.error(f"Erro ao ler cache_site: {e}")
    return None, None


def _idade_desde(ts_iso: str):
    """Converte ISO em idade em minutos."""
    try:
        dt = datetime.fromisoformat(ts_iso.replace("Z", "+00:00"))
        idade = (datetime.now(timezone.utc) - dt).total_seconds() / 60
        return round(idade, 1), ts_iso
    except Exception:
        return None, None


CHECKS = [
    ("saude_ana_novaera",  "ANA · Nova Era",      _idade_de_ana, (COD_NE,), 120),
    ("saude_ana_timoteo",  "ANA · Timóteo",       _idade_de_ana, (COD_T,),  120),
    ("saude_cemig",        "CEMIG · Sá Carvalho", _idade_de_cemig, (),      180),
    ("saude_cache",        "Cache do site",       _idade_de_cache, (),      120),
]



# HELPERS

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
        return r.status_code == 200
    except Exception as e:
        log.error(f"Erro Telegram: {e}")
        return False


def classificar(idade_min: Optional[float], limite_min: float) -> str:
    """
    Classifica a idade do dado:
      - OK      : dentro do limite
      - ATRASADO: até 2x o limite
      - PARADO  : mais que 2x o limite
      - SEM_DADO: nunca chegou
    """
    if idade_min is None:
        return "sem_dado"
    if idade_min <= limite_min:
        return "ok"
    if idade_min <= limite_min * 2:
        return "atrasado"
    return "parado"


def formatar_idade(minutos: Optional[float]) -> str:
    if minutos is None:
        return "sem dado"
    if minutos < 60:
        return f"há {int(minutos)} min"
    horas = minutos / 60
    if horas < 24:
        return f"há {horas:.1f}h"
    dias = horas / 24
    return f"há {dias:.1f} dias"


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
        log.error(f"Erro gravando estado {chave}: {e}")



# MENSAGENS

def montar_alerta(nome: str, nova_faixa: str, idade_min: float,
                  ts_iso: str, limite_min: float) -> str:
    if nova_faixa == "parado":
        emoji = "🔴"
        titulo = "PARADO"
    elif nova_faixa == "atrasado":
        emoji = "🟡"
        titulo = "ATRASADO"
    elif nova_faixa == "sem_dado":
        emoji = "🔴"
        titulo = "SEM DADOS"
    else:
        emoji = "🟢"
        titulo = "NORMALIZADO"

    linhas = [
        f"{emoji} <b>MONITORAMENTO — {titulo}</b>",
        "",
        f"📌 <b>{nome}</b>",
    ]

    if ts_iso:
        dt = datetime.fromisoformat(ts_iso.replace("Z", "+00:00"))
        dt_brt = dt.astimezone(TZ_BRASILIA).strftime("%d/%m %H:%M")
        linhas.append(f"Última leitura: {dt_brt} ({formatar_idade(idade_min)})")
    else:
        linhas.append("Nunca chegou dado nesta tabela")

    if nova_faixa != "ok":
        linhas += [
            "",
            f"Limite esperado: {int(limite_min/60)}h",
            f"Verifique: https://github.com/ryandevbr/Alerta_Enchente_CDV/actions",
        ]

    return "\n".join(linhas)



# PROCESSAMENTO

def processar_check(chave: str, nome: str, funcao, args, limite_min: float) -> bool:
    # Obtém idade atual
    idade_min, ts_iso = funcao(*args)
    faixa_nova = classificar(idade_min, limite_min)

    # Estado anterior
    estado = obter_estado(chave)
    faixa_antiga = estado["ultima_faixa"] if estado else "inicial"

    # Log
    idade_str = formatar_idade(idade_min)
    log.info(f"   {nome}: {idade_str} → {faixa_nova} (anterior: {faixa_antiga})")

    # Não mudou nada — não notifica
    if faixa_nova == faixa_antiga:
        gravar_estado(chave, faixa_nova, idade_min or 0, False)
        return False

    # Primeira vez rodando o monitor
    if estado is None:
        # Só notifica se já está em problema na primeira execução
        notificar = faixa_nova in ("atrasado", "parado", "sem_dado")
        if notificar:
            msg = montar_alerta(nome, faixa_nova, idade_min, ts_iso, limite_min)
            if enviar_telegram(msg):
                log.info(f"   Alerta inicial enviado ({faixa_nova})")
        gravar_estado(chave, faixa_nova, idade_min or 0, notificar)
        return notificar

    # Mudou para melhor (normalizou)
    if faixa_nova == "ok":
        msg = montar_alerta(nome, "ok", idade_min, ts_iso, limite_min)
        ok = enviar_telegram(msg)
        gravar_estado(chave, faixa_nova, idade_min or 0, ok)
        if ok:
            log.info(f"   Notificação de normalização enviada")
        return ok

    # Piorou ou mudou para outro estado ruim — notifica
    msg = montar_alerta(nome, faixa_nova, idade_min, ts_iso, limite_min)
    ok = enviar_telegram(msg)
    gravar_estado(chave, faixa_nova, idade_min or 0, ok)
    if ok:
        log.info(f"   Alerta enviado: {faixa_antiga} → {faixa_nova}")
    return ok

def verificar_camera(supabase):
    """
    Verifica se a camera ao vivo esta offline e alerta.
    Usa a chave cache_site.camera_status gravada pelo atualizador.
    """
    try:
        resp = (
            supabase.table("cache_site")
            .select("dados,atualizado_em")
            .eq("chave", "camera_status")
            .limit(1)
            .execute()
        )
    except Exception as e:
        return None, f"Erro ao ler camera_status: {e}"

    if not resp.data:
        return None, "camera_status ausente no cache"

    row = resp.data[0]
    dados = row["dados"]
    atualizado_em = row["atualizado_em"]
    ao_vivo = dados.get("ao_vivo", False)

    if ao_vivo:
        return "ok", "Camera ao vivo"

    # Offline — calcula ha quanto tempo
    dt_atual = datetime.fromisoformat(atualizado_em.replace("Z", "+00:00"))
    minutos = int((datetime.now(timezone.utc) - dt_atual).total_seconds() / 60)

    if minutos >= 30:
        return "alerta", f"Camera offline ha {minutos} min"
    else:
        return "ok", f"Camera offline ha {minutos} min (abaixo do limiar)"

# MAIN

def main() -> int:
    log.info("=" * 60)
    log.info("MONITORAMENTO DE SAÚDE")
    log.info("=" * 60)

    alertas = 0
    for chave, nome, funcao, args, limite in CHECKS:
        try:
            if processar_check(chave, nome, funcao, args, limite):
                alertas += 1
        except Exception as e:
            log.exception(f"Erro no check {nome}: {e}")

    log.info(f"Fim — {alertas} notificação(ões) enviada(s)")
    return 0

    # Verifica camera
    status_cam, msg_cam = verificar_camera(supabase)
    if status_cam == "alerta":
        # Envia Telegram (use a função que já existe no monitor_saude.py)
        enviar_telegram(f"⚠️ {msg_cam}")
        log.warning(msg_cam)
    elif status_cam == "ok":
        log.info(msg_cam)

if __name__ == "__main__":
    sys.exit(main())
