"""Configurações centralizadas, constantes hidrológicas e logging."""

import logging
import os
import sys
from datetime import timedelta, timezone
from typing import Optional

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# Timezone oficial do Brasil (a ANA e a CEMIG publicam horários em Brasília)
TZ_BRASILIA = timezone(timedelta(hours=-3))

# Códigos das estações fluviométricas da ANA
COD_NOVA_ERA = "56661000"     # Nova Era (montante principal)
COD_TIMOTEO = "56696000"      # Timóteo / Cachoeira do Vale (jusante)
COD_GUILMAN = "56675080"      # UHE Guilman Jusante (montante intermediário)
COD_SA_CARVALHO = "56688080"  # Sá Carvalho Barramento (proxy operacional)

# Lag da onda de cheia entre Nova Era e Timóteo, em horas.
# O estudo hidrológico concluiu que a mediana real é 10h, mas o frontend
# atual está com slider max=9 (rótulo "+9h Lido em Nova Era"). Enquanto
# o HTML não for atualizado, manter este valor em sincronia com o slider.
LAG_HORAS = 9

# Conjuntos de estações por contexto operacional
ESTACOES_ANA_COMPLETAS: list[str] = [
    COD_TIMOTEO,
    COD_NOVA_ERA,
    COD_GUILMAN,
    COD_SA_CARVALHO,
]

# Estações coletadas via SOAP/XML: (código, nome amigável)
ESTACOES_ANA_XML: list[tuple[str, str]] = [
    (COD_NOVA_ERA, "Nova Era"),
    (COD_TIMOTEO, "Timóteo"),
]

# Estações monitoradas por alerta: (código, nome amigável, lag até Timóteo em h)
ESTACOES_ANA_ALERTA: list[tuple[str, str, int]] = [
    (COD_NOVA_ERA, "Nova Era", LAG_HORAS),
    (COD_TIMOTEO, "Timóteo", 0),
]

# Endereço do servidor MediaMTX (câmera ao vivo no VPS)
VPS_IP = os.getenv("VPS_IP", "82.27.38.143")
VPS_CAMERA_URL = os.getenv(
    "VPS_CAMERA_URL",
    f"http://{VPS_IP}:9997/v3/paths/list",
)


def validar_env(*nomes_variaveis: str) -> dict[str, str]:
    """
    Valida a presença das variáveis de ambiente informadas.
    Encerra o processo com código 1 caso alguma esteja ausente ou vazia.
    """
    valores: dict[str, str] = {}
    faltando: list[str] = []

    for nome in nomes_variaveis:
        valor = os.getenv(nome)
        if not valor:
            faltando.append(nome)
        else:
            valores[nome] = valor

    if faltando:
        print(f"Faltam variáveis no .env: {', '.join(faltando)}", file=sys.stderr)
        sys.exit(1)

    return valores


def configurar_logger(
    nome: str,
    arquivo_log: Optional[str] = None,
    formato_data: str = "%Y-%m-%d %H:%M:%S",
) -> logging.Logger:
    """
    Configura o logger padrão do projeto e silencia logs verbosos do httpx/httpcore.
    """
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    if arquivo_log:
        handlers.insert(0, logging.FileHandler(arquivo_log, encoding="utf-8"))

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt=formato_data,
        handlers=handlers,
        force=True,
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    return logging.getLogger(nome)
