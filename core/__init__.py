"""Pacote core compartilhado do sistema Alerta Enchente CDV."""

from core.config import (
    COD_GUILMAN,
    COD_NOVA_ERA,
    COD_SA_CARVALHO,
    COD_TIMOTEO,
    ESTACOES_ANA_ALERTA,
    ESTACOES_ANA_COMPLETAS,
    ESTACOES_ANA_XML,
    LAG_HORAS,
    TZ_BRASILIA,
    configurar_logger,
    validar_env,
)
from core.db import obter_supabase

__all__ = [
    "COD_GUILMAN",
    "COD_NOVA_ERA",
    "COD_SA_CARVALHO",
    "COD_TIMOTEO",
    "ESTACOES_ANA_ALERTA",
    "ESTACOES_ANA_COMPLETAS",
    "ESTACOES_ANA_XML",
    "LAG_HORAS",
    "TZ_BRASILIA",
    "configurar_logger",
    "obter_supabase",
    "validar_env",
]

