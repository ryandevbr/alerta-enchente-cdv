"""Acesso ao Supabase e operações compartilhadas de banco de dados."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Optional

from core.config import validar_env

if TYPE_CHECKING:
    from supabase import Client

log = logging.getLogger(__name__)

_supabase_client: Optional[Client] = None


def obter_supabase() -> Client:
    """Retorna instância singleton do cliente Supabase."""
    global _supabase_client
    if _supabase_client is None:
        from supabase import create_client

        env = validar_env("SUPABASE_URL", "SUPABASE_KEY")
        _supabase_client = create_client(env["SUPABASE_URL"], env["SUPABASE_KEY"])
    return _supabase_client


def parse_iso_utc(ts_iso: str) -> Optional[datetime]:
    """Converte string ISO-8601 (com ou sem sufixo 'Z') para datetime em UTC."""
    if not ts_iso:
        return None
    try:
        dt = datetime.fromisoformat(ts_iso.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except (ValueError, TypeError, AttributeError):
        return None


def calcular_idade_minutos(ts_iso: Optional[str]) -> Optional[float]:
    """Calcula a idade em minutos de um timestamp ISO-8601 até o instante atual."""
    dt = parse_iso_utc(ts_iso or "")
    if dt is None:
        return None
    return (datetime.now(timezone.utc) - dt).total_seconds() / 60.0


def obter_ultima_leitura_ana(
    codigo: str,
    apenas_com_nivel: bool = True,
    fonte: Optional[str] = None,
    client: Optional[Client] = None,
) -> Optional[dict[str, Any]]:
    """
    Busca o registro mais recente de uma estação em `historico_ana`.
    Retorna dict com `data_hora` e `nivel_cm`, ou None se indisponível.
    """
    db = client or obter_supabase()
    try:
        query = (
            db.table("historico_ana")
            .select("data_hora,nivel_cm")
            .eq("codigo_ana", codigo)
        )
        if apenas_com_nivel:
            query = query.not_.is_("nivel_cm", "null")
        if fonte:
            query = query.eq("fonte", fonte)

        resp = query.order("data_hora", desc=True).limit(1).execute()
        if resp.data:
            return resp.data[0]
    except Exception as exc:
        log.error("Erro ao consultar última leitura ANA (%s): %s", codigo, exc)
    return None


def obter_ultima_leitura_cemig(
    apenas_com_defluencia: bool = False,
    client: Optional[Client] = None,
) -> Optional[dict[str, Any]]:
    """
    Busca o registro mais recente em `historico_cemig`.
    """
    db = client or obter_supabase()
    try:
        query = db.table("historico_cemig").select(
            "data_hora,defluencia_m3s,afluencia_m3s,volume_util_pct"
        )
        if apenas_com_defluencia:
            query = query.not_.is_("defluencia_m3s", "null")

        resp = query.order("data_hora", desc=True).limit(1).execute()
        if resp.data:
            return resp.data[0]
    except Exception as exc:
        log.error("Erro ao consultar historico_cemig: %s", exc)
    return None


def obter_estado_alerta(
    chave: str,
    client: Optional[Client] = None,
) -> Optional[dict[str, Any]]:
    """Lê o estado anterior de alerta/monitoramento em `alertas_estado`."""
    db = client or obter_supabase()
    try:
        resp = (
            db.table("alertas_estado")
            .select("*")
            .eq("estacao", chave)
            .limit(1)
            .execute()
        )
        if resp.data:
            return resp.data[0]
    except Exception as exc:
        log.debug("Não foi possível ler alertas_estado (%s): %s", chave, exc)
    return None


def gravar_estado_alerta(
    chave: str,
    faixa: str,
    valor: float,
    notificou: bool,
    client: Optional[Client] = None,
) -> None:
    """Atualiza (upsert) o estado em `alertas_estado`."""
    db = client or obter_supabase()
    agora = datetime.now(timezone.utc).isoformat()
    payload: dict[str, Any] = {
        "estacao": chave,
        "ultima_faixa": faixa,
        "ultimo_nivel": valor,
        "ultima_mudanca": agora,
    }
    if notificou:
        payload["notificado_em"] = agora

    try:
        db.table("alertas_estado").upsert(payload, on_conflict="estacao").execute()
    except Exception as exc:
        log.error("Erro ao gravar estado '%s': %s", chave, exc)


def gravar_cache_site(
    chave: str,
    dados: dict[str, Any],
    client: Optional[Client] = None,
) -> bool:
    """Faz upsert de uma chave em `cache_site`."""
    db = client or obter_supabase()
    try:
        db.table("cache_site").upsert(
            {
                "chave": chave,
                "dados": dados,
                "atualizado_em": datetime.now(timezone.utc).isoformat(),
            },
            on_conflict="chave",
        ).execute()
        return True
    except Exception as exc:
        log.error("Falha ao gravar cache '%s': %s", chave, exc)
        return False


def registrar_sync_status(
    estacoes_sincronizadas: int,
    registros_inseridos: int,
    duracao_segundos: float,
    sucesso: bool,
    erro: Optional[str] = None,
    silencioso: bool = False,
    client: Optional[Client] = None,
) -> None:
    """Grava um registro de execução na tabela `sync_status`."""
    db = client or obter_supabase()
    try:
        db.table("sync_status").insert(
            {
                "estacoes_sincronizadas": estacoes_sincronizadas,
                "registros_inseridos": registros_inseridos,
                "duracao_segundos": round(duracao_segundos, 2),
                "sucesso": sucesso,
                "erro": erro,
            }
        ).execute()
    except Exception as exc:
        if not silencioso:
            log.error("Falha ao gravar sync_status: %s", exc)
