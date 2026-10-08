# ATUALIZADOR DE CACHE — Site Público Alerta Enchente CDV
#
# Lê `historico_ana` e produz os dois objetos JSON consumidos pelo
# index_base.html em `cache_site`:
#
#   status_atual    → { nivel_atual, ultima_leitura }      (Timóteo agora)
#   onda_desfasada  → { 1: ..., 2: ..., ..., 9: ... }      (onda em trânsito)
#
# A onda é calculada a partir da série da Nova Era deslocada pelo lag.
# Lag nominal: 9 horas (ver nota abaixo sobre o estudo hidrológico).
#
# Autor: Ryan Lucas de Freitas Martins
# Licença: MIT

import os
import sys
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional
import requests
import pandas as pd
from dotenv import load_dotenv
from supabase import create_client, Client


# CONFIGURAÇÃO

load_dotenv()

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

if not SUPABASE_URL or not SUPABASE_KEY:
    print("Faltam SUPABASE_URL e/ou SUPABASE_KEY no .env")
    sys.exit(1)

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)


# PARÂMETROS OPERACIONAIS

# Código ANA das estações (iguais aos usados no atualizador_tempo_real.py)
COD_MONTANTE = "56661000"   # Nova Era
COD_JUSANTE  = "56696000"   # Timóteo (Cachoeira do Vale)

# Lag da onda de cheia, em horas.
#
# O estudo hidrológico concluiu que a mediana real é 10h, mas o frontend
# atual está com slider max=9 (rótulo "+9h Lido em Nova Era"). Enquanto
# o HTML não for atualizado, manter este valor em sincronia com o slider.
#
# Quando decidir migrar para 10h:
#   1. Mude este valor para 10
#   2. No index_base.html, mude <input max="9"> para max="10"
#   3. Ajuste o rótulo "+9h (Lido em Nova Era)" para "+10h"
LAG_HORAS = 9

# Tolerância (em horas) para encontrar uma leitura próxima do alvo.
# A ANA às vezes pula leituras; aceitamos valores até 2h de distância.
TOLERANCIA_BUSCA_HORAS = 2


# LOGGING

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.FileHandler("atualizador_cache.log", encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("cache")

logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)


# COLETA — TIMÓTEO (status atual)

def obter_status_atual() -> Optional[dict]:
    """
    Retorna o nível mais recente de Timóteo + dados CEMIG (se disponíveis).
    Saída: { "nivel_atual": float, "ultima_leitura": ISO-8601 UTC,
             "defluencia_cemig": float | None,
             "afluencia_cemig": float | None,
             "volume_util_cemig": float | None,
             "cemig_atualizado_em": ISO | None }
    """
    # ---- Timóteo (ANA) ----
    try:
        resp = (
            supabase.table("historico_ana")
            .select("data_hora,nivel_cm")
            .eq("codigo_ana", COD_JUSANTE)
            .not_.is_("nivel_cm", "null")
            .order("data_hora", desc=True)
            .limit(1)
            .execute()
        )
    except Exception as e:
        log.error(f"Erro ao consultar Timóteo: {e}")
        return None

    if not resp.data:
        log.warning("Sem dados recentes de Timóteo em historico_ana")
        return None

    r = resp.data[0]
    status = {
        "nivel_atual":    round(float(r["nivel_cm"]), 1),
        "ultima_leitura": r["data_hora"],
        "defluencia_cemig":     None,
        "afluencia_cemig":      None,
        "volume_util_cemig":    None,
        "cemig_atualizado_em":  None,
    }

    # ---- CEMIG (Sá Carvalho) ----
    try:
        rc = (
            supabase.table("historico_cemig")
            .select("data_hora,defluencia_m3s,afluencia_m3s,volume_util_pct")
            .order("data_hora", desc=True)
            .limit(1)
            .execute()
        )
        if rc.data:
            c = rc.data[0]
            status["defluencia_cemig"]    = c.get("defluencia_m3s")
            status["afluencia_cemig"]     = c.get("afluencia_m3s")
            status["volume_util_cemig"]   = c.get("volume_util_pct")
            status["cemig_atualizado_em"] = c.get("data_hora")
    except Exception as e:
        log.warning(f"Sem dados CEMIG: {e}")

    return status


# COLETA — NOVA ERA (série para o cálculo da onda desfasada)

def obter_serie_montante(horas_atras: int) -> Optional[pd.DataFrame]:
    """
    Retorna DataFrame horário (resample 1h, ffill) com a série recente de
    Nova Era. Índice em UTC. Coluna: nivel_cm.
    """
    agora = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    desde = agora - timedelta(hours=horas_atras + 2)

    try:
        resp = (
            supabase.table("historico_ana")
            .select("data_hora,nivel_cm")
            .eq("codigo_ana", COD_MONTANTE)
            .gte("data_hora", desde.isoformat())
            .not_.is_("nivel_cm", "null")
            .order("data_hora")
            .execute()
        )
    except Exception as e:
        log.error(f"Erro ao consultar Nova Era: {e}")
        return None

    if not resp.data:
        log.warning(f"Sem dados de Nova Era nas últimas {horas_atras + 2}h")
        return None

    df = pd.DataFrame(resp.data)
    df["data_hora"] = pd.to_datetime(df["data_hora"], utc=True)
    df = df.set_index("data_hora").sort_index()

    # Reamostra para hora cheia. Se houver múltiplas leituras na mesma hora
    # (ex.: 15 min ou 30 min), pega a mais recente.
    df_hourly = df.resample("1h").last().ffill()

    return df_hourly

def gerar_historico_rio_7d(supabase, estacao="56696000", nome="MARIO DE CARVALHO"):
    """
    Gera a chave historico_rio_7d do cache_site.
    7 dias de leituras da ANA, agregadas por hora (maximo).
    Inclui dados provisorios (originados do coletor XML) para cobertura continua.
    """
    limite = datetime.now(timezone.utc) - timedelta(days=7)

    resp = (
        supabase.table("historico_ana")
        .select("data_hora,nivel_cm")
        .eq("codigo_ana", estacao)
        .gte("data_hora", limite.isoformat())
        .order("data_hora", desc=False)
        .execute()
    )

    if not resp.data:
        return None

    # Agrega por hora (maximo)
    buckets = {}
    for row in resp.data:
        dt = datetime.fromisoformat(row["data_hora"].replace("Z", "+00:00"))
        chave = dt.replace(minute=0, second=0, microsecond=0)
        nivel = float(row["nivel_cm"])
        if chave not in buckets or nivel > buckets[chave]:
            buckets[chave] = nivel

    pontos = [
        { "t": k.isoformat().replace("+00:00", "Z"), "v": round(v, 1) }
        for k, v in sorted(buckets.items())
    ]

    if not pontos:
        return None

    valores = [p["v"] for p in pontos]
    nivel_atual = valores[-1]

    limite_24h = datetime.now(timezone.utc) - timedelta(hours=24)
    pontos_24h = [p["v"] for p in pontos
                  if datetime.fromisoformat(p["t"].replace("Z", "+00:00")) >= limite_24h]
    tendencia = round(nivel_atual - pontos_24h[0], 1) if len(pontos_24h) >= 2 else None

    return {
        "estacao": estacao,
        "nome": nome,
        "gerado_em": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "pontos": pontos,
        "min": round(min(valores), 1),
        "max": round(max(valores), 1),
        "tendencia_24h": tendencia,
        "referencias": { "atencao": 450, "alerta": 540, "inundacao": 620 },
    }

# CONSTRUÇÃO DA ONDA DESFASADA

def construir_onda_desfasada(nivel_timoteo_atual: float) -> Optional[dict]:
    """
    Monta { 1: previsao_T, 2: previsao_T, ..., LAG: previsao_T } usando
    o método da VARIAÇÃO (delta) de Nova Era aplicada sobre Timóteo atual.

    Fórmula:
        delta(n) = NE(agora - LAG + n) - NE(agora - LAG)
        onda[n]  = T_agora + delta(n)

    Isso remove o problema de escalas absolutas diferentes entre as réguas.
    """
    if nivel_timoteo_atual is None:
        log.warning("Nível de Timóteo indisponível. Onda não calculada.")
        return None

    df = obter_serie_montante(LAG_HORAS)
    if df is None or df.empty:
        return None

    agora = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    alvo_base = agora - timedelta(hours=LAG_HORAS)

    # Passo 1: nível de Nova Era LAG horas atrás (ponto de referência)
    janela_base = df[
        (df.index >= alvo_base - timedelta(hours=TOLERANCIA_BUSCA_HORAS))
        & (df.index <= alvo_base + timedelta(hours=TOLERANCIA_BUSCA_HORAS))
    ]
    if janela_base.empty:
        log.warning(f"Sem leitura de Nova Era em ~{alvo_base}. Onda não calculada.")
        return None

    pos_base = abs((janela_base.index - alvo_base).total_seconds()).argmin()
    ne_base = janela_base.iloc[pos_base]["nivel_cm"]
    if pd.isna(ne_base):
        log.warning("NE base inválida.")
        return None

    log.debug(f"   NE base (há {LAG_HORAS}h): {ne_base:.1f} cm")

    # Passo 2: para cada hora n, calcula o delta até este ponto
    onda = {}
    for n in range(1, LAG_HORAS + 1):
        alvo_n = agora - timedelta(hours=(LAG_HORAS - n))

        janela_n = df[
            (df.index >= alvo_n - timedelta(hours=TOLERANCIA_BUSCA_HORAS))
            & (df.index <= alvo_n + timedelta(hours=TOLERANCIA_BUSCA_HORAS))
        ]
        if janela_n.empty:
            log.debug(f"   Sem leitura em {alvo_n} para onda[{n}]")
            continue

        pos_n = abs((janela_n.index - alvo_n).total_seconds()).argmin()
        ne_n = janela_n.iloc[pos_n]["nivel_cm"]
        if pd.isna(ne_n):
            continue

        delta = ne_n - ne_base
        previsao = nivel_timoteo_atual + delta
        onda[n] = round(float(previsao), 1)

    if not onda:
        log.warning("Nenhum valor calculado para a onda.")
        return None

    return onda


# COMPARATIVO ANUAL — nível de Timóteo no mesmo dia/hora em anos anteriores

def construir_comparativo_anual() -> Optional[dict]:
    """
    Consulta o histórico de Timóteo e devolve o nível no mesmo dia/hora
    nos últimos 8 anos, mais estatísticas (média, mediana, min, max).

    Retorna algo como:
    {
      "dia_mes": "23/09",
      "hora": "14:00",
      "atual": { "ano": 2026, "nivel_cm": 95.0, "timestamp": "..." },
      "anos_anteriores": [
        { "ano": 2019, "nivel_cm": 132.0 },
        { "ano": 2020, "nivel_cm": 145.0 },
        ...
      ],
      "estatisticas": { "media": 112.3, "mediana": 112.0, "min": 72.0, "max": 150.0 }
    }
    """
    from datetime import datetime as _dt
    from statistics import mean, median

    try:
        # ---- 1. Última leitura de Timóteo (referência) ----
        resp = (
            supabase.table("historico_ana")
            .select("data_hora,nivel_cm")
            .eq("codigo_ana", COD_JUSANTE)
            .not_.is_("nivel_cm", "null")
            .order("data_hora", desc=True)
            .limit(1)
            .execute()
        )
        if not resp.data:
            log.warning("Comparativo: sem dados recentes de Timóteo")
            return None

        ref = resp.data[0]
        ref_dt = _dt.fromisoformat(ref["data_hora"].replace("Z", "+00:00"))
        ref_nivel = float(ref["nivel_cm"])
        ref_ano = ref_dt.year
        ref_mes = ref_dt.month
        ref_dia = ref_dt.day
        ref_hora = ref_dt.hour

        log.info(f"Comparativo: referência {ref_dia:02d}/{ref_mes:02d} "
                 f"{ref_hora:02d}h · {ref_nivel:.0f} cm ({ref_ano})")

        # ---- 2. Para cada ano anterior, busca o registro mais próximo ----
        # Tolerância de ±3h para o horário e ±1 dia para a data
        TOLERANCIA_HORAS = 3

        anos_anteriores = []
        for ano in range(2019, ref_ano):
            # Monta a janela: mesmo dia/hora no ano X, com tolerância
            try:
                alvo = ref_dt.replace(year=ano)
            except ValueError:
                # 29/02 em ano não bissexto — pula
                continue

            # Janela de busca: ±3h do horário alvo
            janela_ini = (alvo - timedelta(hours=TOLERANCIA_HORAS)).isoformat()
            janela_fim = (alvo + timedelta(hours=TOLERANCIA_HORAS)).isoformat()

            try:
                r = (
                    supabase.table("historico_ana")
                    .select("data_hora,nivel_cm")
                    .eq("codigo_ana", COD_JUSANTE)
                    .not_.is_("nivel_cm", "null")
                    .gte("data_hora", janela_ini)
                    .lte("data_hora", janela_fim)
                    .order("data_hora")
                    .limit(50)
                    .execute()
                )
            except Exception as e:
                log.warning(f"   Ano {ano}: erro na consulta ({e})")
                continue

            if not r.data:
                log.debug(f"   Ano {ano}: sem dado na janela")
                continue

            # Escolhe o registro com data_hora mais próxima do alvo
            def diff_minutos(item):
                dt = _dt.fromisoformat(item["data_hora"].replace("Z", "+00:00"))
                return abs((dt - alvo).total_seconds())

            mais_proximo = min(r.data, key=diff_minutos)
            nivel = float(mais_proximo["nivel_cm"])

            anos_anteriores.append({
                "ano": ano,
                "nivel_cm": round(nivel, 1),
            })

        # ---- 3. Estatísticas ----
        if anos_anteriores:
            valores = [a["nivel_cm"] for a in anos_anteriores]
            estatisticas = {
                "media":   round(mean(valores), 1),
                "mediana": round(median(valores), 1),
                "min":     round(min(valores), 1),
                "max":     round(max(valores), 1),
            }
        else:
            estatisticas = {"media": None, "mediana": None, "min": None, "max": None}

        return {
            "dia_mes": f"{ref_dia:02d}/{ref_mes:02d}",
            "hora":    f"{ref_hora:02d}:00",
            "atual": {
                "ano":       ref_ano,
                "nivel_cm":  round(ref_nivel, 1),
                "timestamp": ref["data_hora"],
            },
            "anos_anteriores": anos_anteriores,
            "estatisticas":    estatisticas,
        }

    except Exception as e:
        log.error(f"Erro no comparativo anual: {e}")
        return None


# DEFLUÊNCIA 48H — série para o gráfico sparkline do site

def construir_defluencia_48h() -> Optional[dict]:
    """
    Consulta historico_cemig dos últimos 2 dias e devolve uma série compacta
    para o frontend desenhar o sparkline.

    Retorna:
    {
      "inicio": "2026-09-22T16:00:00+00:00",
      "fim":    "2026-09-24T16:00:00+00:00",
      "pontos": [ { "t": "...", "v": 17.4 }, ... ],
      "min": 15.2,
      "max": 32.1,
      "atual": 26.4,
      "variacao_pct": 51.7
    }
    """
    from datetime import datetime as _dt

    agora = datetime.now(timezone.utc)
    desde = agora - timedelta(hours=48)

    try:
        r = (
            supabase.table("historico_cemig")
            .select("data_hora,defluencia_m3s")
            .not_.is_("defluencia_m3s", "null")
            .gte("data_hora", desde.isoformat())
            .lte("data_hora", agora.isoformat())
            .order("data_hora")
            .limit(500)
            .execute()
        )
    except Exception as e:
        log.error(f"Erro ao consultar defluência 48h: {e}")
        return None

    if not r.data:
        log.warning("Sem dados de defluência nas últimas 48h")
        return None

    pontos = [
        {
            "t": item["data_hora"],
            "v": round(float(item["defluencia_m3s"]), 2),
        }
        for item in r.data
        if item.get("defluencia_m3s") is not None
    ]

    if not pontos:
        return None

    valores = [p["v"] for p in pontos]
    atual = valores[-1]
    primeiro = valores[0]
    variacao = ((atual - primeiro) / primeiro * 100) if primeiro > 0 else 0

    return {
        "inicio":  pontos[0]["t"],
        "fim":     pontos[-1]["t"],
        "pontos":  pontos,
        "min":     round(min(valores), 1),
        "max":     round(max(valores), 1),
        "atual":   round(atual, 1),
        "variacao_pct": round(variacao, 1),
    }

# ESCRITA NO CACHE

def gravar_cache(chave: str, dados: dict) -> bool:
    """
    Faz upsert de uma chave em cache_site.
    Retorna True em sucesso, False caso contrário.
    """
    try:
        supabase.table("cache_site").upsert(
            {
                "chave":         chave,
                "dados":         dados,
                "atualizado_em": datetime.now(timezone.utc).isoformat(),
            },
            on_conflict="chave",
        ).execute()
        return True
    except Exception as e:
        log.error(f"Falha ao gravar cache '{chave}': {e}")
        return False



# HEALTH CHECK

def registrar_status(sucesso: bool, erro: Optional[str] = None) -> None:
    """Grava um registro em sync_status se a tabela existir."""
    try:
        supabase.table("sync_status").insert({
            "estacoes_sincronizadas": 2 if sucesso else 0,
            "registros_inseridos":    0,
            "duracao_segundos":       0,
            "sucesso":                sucesso,
            "erro":                   f"[cache] {erro}" if erro else "[cache] ok",
        }).execute()
    except Exception:
        pass  # tabela pode não existir — não crítico

# STATUS DA CAMERA AO VIVO (YouTube)
def obter_status_camera() -> Optional[dict]:
    """
    Consulta a API do YouTube para saber se o canal esta ao vivo.

    Usa videos.list com liveStreamingDetails.concurrentViewers, que so
    esta presente quando o video esta realmente ao vivo. Mais confiavel
    que search.list com eventType=live (que tem lag de indexacao).

    Custo: 100 (search) + 1 (videos.list) = 101 unidades por verificacao.
    """
    api_key = os.getenv("YOUTUBE_API_KEY")
    channel_id = os.getenv("YOUTUBE_CHANNEL_ID", "UCNvHQaWlCTC07bfEvL2eqQA")

    if not api_key:
        log.warning("YOUTUBE_API_KEY ausente - status da camera nao verificado")
        return None

    # Passo 1: pega os 5 videos mais recentes do canal
    try:
        r = requests.get(
            "https://www.googleapis.com/youtube/v3/search",
            params={
                "part": "id",
                "channelId": channel_id,
                "type": "video",
                "order": "date",
                "maxResults": 5,
                "key": api_key,
            },
            timeout=10,
        )
        r.raise_for_status()
        items = r.json().get("items", [])
    except Exception as e:
        log.error(f"Erro no search.list: {e}")
        return None

    if not items:
        # Canal sem videos nenhum — improvavel, mas trata
        return {
            "ao_vivo": False,
            "verificado_em": datetime.now(timezone.utc).isoformat(),
        }

    video_ids = [
        it["id"]["videoId"]
        for it in items
        if isinstance(it.get("id"), dict) and "videoId" in it["id"]
    ]

    if not video_ids:
        return {
            "ao_vivo": False,
            "verificado_em": datetime.now(timezone.utc).isoformat(),
        }

    # Passo 2: verifica quais desses videos estao ao vivo
    try:
        r2 = requests.get(
            "https://www.googleapis.com/youtube/v3/videos",
            params={
                "part": "liveStreamingDetails",
                "id": ",".join(video_ids),
                "key": api_key,
            },
            timeout=10,
        )
        r2.raise_for_status()
        detalhes = r2.json().get("items", [])
    except Exception as e:
        log.error(f"Erro no videos.list: {e}")
        return None

    ao_vivo = False
    for item in detalhes:
        d = item.get("liveStreamingDetails", {})
        # concurrentViewers so existe quando o video esta ao vivo
        # (fallback: tem actualStartTime mas nao tem actualEndTime)
        if "concurrentViewers" in d:
            ao_vivo = True
            break
        if d.get("actualStartTime") and not d.get("actualEndTime"):
            ao_vivo = True
            break

    return {
        "ao_vivo": ao_vivo,
        "verificado_em": datetime.now(timezone.utc).isoformat(),
    }

# ENTRYPOINT

def main() -> int:
    log.info("=" * 60)
    log.info("ATUALIZANDO CACHE DO SITE PÚBLICO")
    log.info(f"   Lag configurado: {LAG_HORAS}h")
    log.info("=" * 60)

    # ---- 1. Status atual (Timóteo + CEMIG) ----
    status = obter_status_atual()
    if status:
        ok = gravar_cache("status_atual", status)
        if ok:
            log.info(f"status_atual → {status['nivel_atual']} cm "
                     f"({status['ultima_leitura']})")
        else:
            registrar_status(False, "falha ao gravar status_atual")
            return 1
    else:
        log.warning("status_atual não foi atualizado (sem dados)")

    # ---- 2. Onda desfasada (Nova Era) ----
    onda = construir_onda_desfasada(status["nivel_atual"] if status else None)
    if onda:
        ok = gravar_cache("onda_desfasada", onda)
        if ok:
            resumo = ", ".join(f"{k}h:{v}" for k, v in sorted(onda.items()))
            log.info(f"onda_desfasada → {resumo}")
        else:
            log.warning("falha ao gravar onda_desfasada")
    else:
        log.warning("onda_desfasada não foi atualizada")

    # ---- 3. Comparativo anual (NOVO) ----
    comparativo = construir_comparativo_anual()
    if comparativo:
        ok = gravar_cache("comparativo_anual", comparativo)
        if ok:
            n = len(comparativo["anos_anteriores"])
            est = comparativo["estatisticas"]
            log.info(f"comparativo_anual → {n} anos | "
                     f"média={est['media']} cm | atual={comparativo['atual']['nivel_cm']} cm")
        else:
            log.warning("falha ao gravar comparativo_anual")
    else:
        log.warning("comparativo_anual não foi atualizado")

    log.info("=" * 60)
    log.info("Cache atualizado com sucesso")
    log.info("=" * 60)

    # ---- 4. Defluência 48h (NOVO) ----
    defluencia = construir_defluencia_48h()
    if defluencia:
        ok = gravar_cache("defluencia_48h", defluencia)
        if ok:
            log.info(f"defluencia_48h → {len(defluencia['pontos'])} pontos "
                     f"({defluencia['min']}–{defluencia['max']} m³/s)")
        else:
            log.warning("falha ao gravar defluencia_48h")
    else:
        log.warning("defluencia_48h não foi atualizada")

    # ---- 5. Histórico do rio 7 dias (NOVO) ----
    historico = gerar_historico_rio_7d(supabase)
    if historico:
        ok = gravar_cache("historico_rio_7d", historico)
        if ok:
            log.info(f"historico_rio_7d → {len(historico['pontos'])} pontos "
                     f"({historico['min']}–{historico['max']} cm) | "
                     f"tendência 24h: {historico['tendencia_24h']} cm")
        else:
            log.warning("falha ao gravar historico_rio_7d")
    else:
        log.warning("historico_rio_7d não foi atualizado")

    # ---- 6. Status da camera ao vivo (NOVO) ----
    camera = obter_status_camera()
    if camera:
        ok = gravar_cache("camera_status", camera)
        if ok:
            estado = "AO VIVO" if camera["ao_vivo"] else "offline"
            log.info(f"camera_status → {estado}")
        else:
            log.warning("falha ao gravar camera_status")
    else:
        log.warning("camera_status nao foi atualizado")

    registrar_status(True)
    return 0


if __name__ == "__main__":
    sys.exit(main())


