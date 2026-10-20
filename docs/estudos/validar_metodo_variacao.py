# VALIDAÇÃO v5 — PREVISÃO DE ΔT COM OLS
# Alerta Enchente CDV
#
# Mudanças em relação ao v4:
#   Target é ΔT(t+k) = T(t+k) − T(t), não T(t+k)
#   OLS puro (Ridge α=0.01) em vez de Ridge α=1.0
#   Previsão final: ŷ = T(t) + ΔT_predito
#   Diagnóstico expandido com baseline "ΔT = 0"
#
# Autor: Ryan Lucas de Freitas Martins
# Licença: MIT

from datetime import datetime, timezone
import json
from pathlib import Path
import pickle
import sys
from typing import Any, Optional

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from sklearn.linear_model import Ridge
from sklearn.metrics import mean_squared_error
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler

# Permite importar `core` mesmo executando a partir de `docs/estudos/`
RAIZ = Path(__file__).resolve().parent.parent.parent
if str(RAIZ) not in sys.path:
    sys.path.insert(0, str(RAIZ))

from core.config import (  # noqa: E402
    COD_GUILMAN as COD_AD_NIVEL,
    COD_NOVA_ERA as COD_NE,
    COD_SA_CARVALHO as COD_BARRA,
    COD_TIMOTEO as COD_T,
    LAG_HORAS,
    configurar_logger,
)
from core.db import obter_supabase  # noqa: E402

log = configurar_logger("v5", formato_data="%H:%M:%S")

HORIZONTES = list(range(1, LAG_HORAS + 1))
SAFRAS = [2018, 2019, 2020, 2021, 2022, 2023, 2024, 2025]
ANO_CORTE = 2023

# OLS essencialmente — alpha quase zero
OLS_ALPHA = 0.01
LIMIAR_DELTA_ATIVO_CM = 40.0

# Cache local para acelerar re-execuções
CACHE_DIR = Path("cache_series")
CACHE_DIR.mkdir(exist_ok=True)


def _cache_path(codigo: str, desde: datetime, ate: datetime) -> Path:
    return CACHE_DIR / f"{codigo}_{desde:%Y%m%d}_{ate:%Y%m%d}.pkl"


def carregar_estacao(
    codigo: str,
    desde: datetime,
    ate: datetime,
    page_size: int = 1000,
    usar_cache: bool = True,
) -> pd.DataFrame:
    """Baixa série histórica paginada do Supabase com cache local em disco."""
    cache_file = _cache_path(codigo, desde, ate)

    if usar_cache and cache_file.exists():
        try:
            with open(cache_file, "rb") as f:
                df = pickle.load(f)
            log.info("   %s: cache (%s leituras)", codigo, f"{len(df):,}")
            return df
        except Exception:
            log.warning(
                "   ⚠️ Cache corrompido para %s. Baixando de novo.", codigo
            )

    supabase = obter_supabase()
    todas: list[dict[str, Any]] = []
    offset = 0

    while True:
        resp = (
            supabase.table("historico_ana")
            .select("data_hora,nivel_cm,chuva_mm,vazao_m3s")
            .eq("codigo_ana", codigo)
            .gte("data_hora", desde.isoformat())
            .lte("data_hora", ate.isoformat())
            .order("data_hora")
            .range(offset, offset + page_size - 1)
            .execute()
        )
        if not resp.data:
            break
        todas.extend(resp.data)
        if len(resp.data) < page_size:
            break
        offset += page_size

    if not todas:
        return pd.DataFrame()

    df = pd.DataFrame(todas)
    df["data_hora"] = pd.to_datetime(df["data_hora"], utc=True)
    df = df.set_index("data_hora").sort_index()

    with open(cache_file, "wb") as f:
        pickle.dump(df, f)
    log.info("   %s: %s leituras (cache salvo)", codigo, f"{len(df):,}")
    return df


def carregar_series() -> tuple[pd.DataFrame, pd.DataFrame]:
    """Carrega, alinha em base horária e calcula deltas para todas as estações."""
    desde = datetime(SAFRAS[0], 12, 1, tzinfo=timezone.utc)
    ate = datetime(SAFRAS[-1] + 1, 3, 1, tzinfo=timezone.utc)

    log.info("⬇️  Dados (%s a %s)", desde.date(), ate.date())
    df_ne = carregar_estacao(COD_NE, desde, ate)
    df_t = carregar_estacao(COD_T, desde, ate)
    df_ad = carregar_estacao(COD_AD_NIVEL, desde, ate)
    df_b = carregar_estacao(COD_BARRA, desde, ate)

    log.info(
        "   NE=%s | T=%s | AD=%s | B=%s",
        f"{len(df_ne):,}",
        f"{len(df_t):,}",
        f"{len(df_ad):,}",
        f"{len(df_b):,}",
    )

    if df_ne.empty or df_t.empty:
        log.error("NE ou T vazia.")
        sys.exit(1)
    if df_ad.empty:
        log.error("%s sem dados.", COD_AD_NIVEL)
        sys.exit(1)

    ne = (
        df_ne.resample("1h")
        .last()
        .ffill()
        .rename(columns={"nivel_cm": "ne"})
    )[["ne"]]
    t = (
        df_t.resample("1h")
        .last()
        .ffill()
        .rename(columns={"nivel_cm": "t"})
    )[["t"]]
    ad = (
        df_ad.resample("1h")
        .last()
        .ffill()
        .rename(
            columns={
                "nivel_cm": "ad_nivel",
                "vazao_m3s": "ad_vazao",
                "chuva_mm": "ad_chuva",
            }
        )
    )[["ad_nivel", "ad_vazao", "ad_chuva"]]

    b = pd.DataFrame()
    if not df_b.empty:
        b = (
            df_b.resample("1h")
            .last()
            .ffill()
            .rename(columns={"nivel_cm": "b_nivel"})
        )[["b_nivel"]]

    df = ne.join(t, how="inner").join(ad, how="left")
    if not b.empty:
        df = df.join(b, how="left")
    else:
        df["b_nivel"] = np.nan

    df = df.dropna(subset=["ne", "t"])

    df["delta_ne"] = df["ne"] - df["ne"].shift(LAG_HORAS)
    df["delta_t"] = df["t"] - df["t"].shift(LAG_HORAS)
    df["delta_ad_nivel"] = df["ad_nivel"] - df["ad_nivel"].shift(LAG_HORAS)
    df["delta_ad_vazao"] = df["ad_vazao"] - df["ad_vazao"].shift(LAG_HORAS)

    for col in ["ad_nivel", "ad_vazao", "ad_chuva", "b_nivel"]:
        df[col] = df[col].ffill()

    df_full = df.copy()

    idx = df.index
    mask_wet = (
        ((idx.month == 12) & (idx.day >= 15))
        | (idx.month == 1)
        | ((idx.month == 2) & (idx.day <= 20))
    )
    df_safra = df[mask_wet].copy()
    df_safra["safra"] = np.where(
        df_safra.index.month == 12,
        df_safra.index.year,
        df_safra.index.year - 1,
    )

    log.info("   Série completa: %s h", f"{len(df_full):,}")
    log.info("   Safras:         %s h", f"{len(df_safra):,}")
    return df_full, df_safra


FEATURES_V3 = ["t", "ne", "delta_ne", "delta_t"]
CONJUNTOS = {
    "v3 (NE + T)": FEATURES_V3,
    "v4a (+ AD nível)": FEATURES_V3 + ["ad_nivel", "delta_ad_nivel"],
    "v4b (+ AD vazão)": FEATURES_V3 + ["ad_vazao", "delta_ad_vazao"],
    "v4c (+ AD nível + vazão)": FEATURES_V3
    + ["ad_nivel", "delta_ad_nivel", "ad_vazao", "delta_ad_vazao"],
    "v4d (+ v4c + barragem)": FEATURES_V3
    + ["ad_nivel", "delta_ad_nivel", "ad_vazao", "delta_ad_vazao", "b_nivel"],
}


def construir_dataset(
    df_full: pd.DataFrame,
    df_safra: pd.DataFrame,
    k: int,
    features: list[str],
) -> tuple[pd.DataFrame, np.ndarray, pd.Index, np.ndarray]:
    """
    Target: ΔT(t+k) = T(t+k) − T(t)
    A previsão final em nível será: ŷ(t+k) = T(t) + ΔT_predito
    """
    x_df = df_full[features].copy()
    y_ser = (df_full["t"].shift(-k) - df_full["t"]).rename("y")

    dataset = pd.concat([x_df, y_ser], axis=1).dropna()
    dataset = dataset.loc[dataset.index.intersection(df_safra.index)].copy()
    dataset["safra"] = df_safra.loc[dataset.index, "safra"]

    return (
        dataset[features],
        dataset["y"].values,
        dataset.index,
        dataset["safra"].values,
    )


def _rmse(y_true: np.ndarray, y_pred: np.ndarray) -> float:
    return float(np.sqrt(mean_squared_error(y_true, y_pred)))


def avaliar(
    df_full: pd.DataFrame,
    df_safra: pd.DataFrame,
    k: int,
    features: list[str],
) -> Optional[dict[str, Any]]:
    """Treina e avalia o modelo OLS/Ridge para o horizonte `k`."""
    x_df, y_delta, idx, safra = construir_dataset(df_full, df_safra, k, features)

    mask_tr = safra <= ANO_CORTE
    mask_te = safra > ANO_CORTE

    x_tr, y_tr = x_df[mask_tr], y_delta[mask_tr]
    x_te, y_te_delta = x_df[mask_te], y_delta[mask_te]

    if len(x_tr) < 100 or len(x_te) < 50:
        return None

    modelo = Pipeline(
        [
            ("scaler", StandardScaler()),
            ("ols", Ridge(alpha=OLS_ALPHA)),
        ]
    )
    modelo.fit(x_tr, y_tr)

    delta_pred = modelo.predict(x_te)
    t_atual = x_te["t"].values
    y_pred_nivel = t_atual + delta_pred
    y_real_nivel = t_atual + y_te_delta

    # Baselines em nível
    y_persist = t_atual
    taxa_t = x_te["delta_t"].values / LAG_HORAS
    y_tend = t_atual + taxa_t * k

    rmse_m = _rmse(y_real_nivel, y_pred_nivel)
    rmse_p = _rmse(y_real_nivel, y_persist)
    rmse_t = _rmse(y_real_nivel, y_tend)

    rmse_delta_m = _rmse(y_te_delta, delta_pred)
    rmse_delta_p = _rmse(y_te_delta, np.zeros_like(y_te_delta))
    rmse_delta_t = _rmse(y_te_delta, taxa_t * k)

    mask_ativo = np.abs(x_te["delta_ne"].values) > LIMIAR_DELTA_ATIVO_CM
    if mask_ativo.sum() >= 20:
        rmse_am = _rmse(y_real_nivel[mask_ativo], y_pred_nivel[mask_ativo])
        rmse_ap = _rmse(y_real_nivel[mask_ativo], y_persist[mask_ativo])
        rmse_at = _rmse(y_real_nivel[mask_ativo], y_tend[mask_ativo])
        rmse_delta_am = _rmse(y_te_delta[mask_ativo], delta_pred[mask_ativo])
        rmse_delta_ap = _rmse(
            y_te_delta[mask_ativo], np.zeros_like(y_te_delta[mask_ativo])
        )
        rmse_delta_at = _rmse(
            y_te_delta[mask_ativo], (taxa_t * k)[mask_ativo]
        )
    else:
        rmse_am = rmse_ap = rmse_at = np.nan
        rmse_delta_am = rmse_delta_ap = rmse_delta_at = np.nan

    return {
        "k": k,
        "n_treino": int(mask_tr.sum()),
        "n_teste": int(mask_te.sum()),
        "n_ativo": int(mask_ativo.sum()),
        "rmse_modelo": rmse_m,
        "rmse_persist": rmse_p,
        "rmse_tend": rmse_t,
        "rmse_a_modelo": rmse_am,
        "rmse_a_persist": rmse_ap,
        "rmse_a_tend": rmse_at,
        "rmse_delta_modelo": rmse_delta_m,
        "rmse_delta_persist": rmse_delta_p,
        "rmse_delta_tend": rmse_delta_t,
        "rmse_delta_a_modelo": rmse_delta_am,
        "rmse_delta_a_persist": rmse_delta_ap,
        "rmse_delta_a_tend": rmse_delta_at,
        "idx_te": idx[mask_te],
        "y_real": y_real_nivel,
        "y_pred": y_pred_nivel,
        "delta_real": y_te_delta,
        "delta_pred": delta_pred,
        "mask_ativo": mask_ativo,
    }


def _imprimir_tabelas(resultados: dict[str, dict[int, dict[str, Any]]]) -> tuple[str, dict[int, dict[str, Any]]]:
    """Imprime as tabelas comparativas de diagnóstico e retorna o melhor conjunto."""
    print("\n" + "=" * 100)
    print("COMPARAÇÃO EM +9h — TESTE TEMPORAL (todas as horas)")
    print("=" * 100)
    print(
        f"{'Conjunto':<30} | {'RMSE mod':>9} | {'RMSE pers':>10} | "
        f"{'RMSE tend':>10} | {'ganho':>9}"
    )
    print("-" * 100)
    for nome, res in resultados.items():
        r = res.get(9)
        if not r:
            continue
        mb = min(r["rmse_persist"], r["rmse_tend"])
        ganho = (1 - r["rmse_modelo"] / mb) * 100
        print(
            f"{nome:<30} | {r['rmse_modelo']:>9.1f} | "
            f"{r['rmse_persist']:>10.1f} | {r['rmse_tend']:>10.1f} | "
            f"{ganho:>+8.1f}%"
        )

    print("\n" + "=" * 100)
    print(
        f"COMPARAÇÃO EM +9h — ONDAS ATIVAS (NÍVEL, |ΔNE| > {LIMIAR_DELTA_ATIVO_CM} cm)"
    )
    print("=" * 100)
    print(
        f"{'Conjunto':<30} | {'n ativo':>8} | {'RMSE mod':>9} | "
        f"{'RMSE pers':>10} | {'RMSE tend':>10} | {'ganho':>9}"
    )
    print("-" * 100)
    for nome, res in resultados.items():
        r = res.get(9)
        if not r or np.isnan(r["rmse_a_modelo"]):
            continue
        mb = min(r["rmse_a_persist"], r["rmse_a_tend"])
        ganho = (1 - r["rmse_a_modelo"] / mb) * 100
        print(
            f"{nome:<30} | {r['n_ativo']:>8} | {r['rmse_a_modelo']:>9.1f} | "
            f"{r['rmse_a_persist']:>10.1f} | {r['rmse_a_tend']:>10.1f} | "
            f"{ganho:>+8.1f}%"
        )

    print("\n" + "=" * 100)
    print(
        f"COMPARAÇÃO EM +9h — ONDAS ATIVAS (ΔT, |ΔNE| > {LIMIAR_DELTA_ATIVO_CM} cm)"
    )
    print("=" * 100)
    print(
        f"{'Conjunto':<30} | {'RMSE ΔT mod':>12} | {'RMSE ΔT pers':>13} | "
        f"{'RMSE ΔT tend':>13} | {'ganho':>9}"
    )
    print("-" * 100)
    for nome, res in resultados.items():
        r = res.get(9)
        if not r or np.isnan(r["rmse_delta_a_modelo"]):
            continue
        mb = min(r["rmse_delta_a_persist"], r["rmse_delta_a_tend"])
        ganho = (1 - r["rmse_delta_a_modelo"] / mb) * 100
        print(
            f"{nome:<30} | {r['rmse_delta_a_modelo']:>12.1f} | "
            f"{r['rmse_delta_a_persist']:>13.1f} | "
            f"{r['rmse_delta_a_tend']:>13.1f} | {ganho:>+8.1f}%"
        )

    def _chave_melhor(item: tuple[str, dict[int, dict[str, Any]]]) -> float:
        _, res = item
        r = res.get(9)
        if not r or np.isnan(r["rmse_a_modelo"]):
            return float("inf")
        return float(r["rmse_a_modelo"])

    melhor_nome, melhor_res = min(resultados.items(), key=_chave_melhor)

    print("\n" + "=" * 100)
    print(f"MELHOR CONJUNTO: {melhor_nome}")
    print("=" * 100)
    print(
        f"\n{'k':>3} | {'RMSE mod':>9} | {'RMSE pers':>10} | "
        f"{'RMSE tend':>10} | {'ganho':>9}"
    )
    print("-" * 60)
    for k in HORIZONTES:
        r = melhor_res.get(k)
        if not r:
            continue
        mb = min(r["rmse_persist"], r["rmse_tend"])
        ganho = (1 - r["rmse_modelo"] / mb) * 100
        print(
            f"{k:>3} | {r['rmse_modelo']:>9.1f} | {r['rmse_persist']:>10.1f} | "
            f"{r['rmse_tend']:>10.1f} | {ganho:>+8.1f}%"
        )

    print("\n[Ondas ativas]")
    print(
        f"{'k':>3} | {'n ativo':>8} | {'RMSE mod':>9} | "
        f"{'RMSE pers':>10} | {'RMSE tend':>10} | {'ganho':>9}"
    )
    print("-" * 90)
    for k in HORIZONTES:
        r = melhor_res.get(k)
        if not r or np.isnan(r["rmse_a_modelo"]):
            continue
        mb = min(r["rmse_a_persist"], r["rmse_a_tend"])
        ganho = (1 - r["rmse_a_modelo"] / mb) * 100
        print(
            f"{k:>3} | {r['n_ativo']:>8} | {r['rmse_a_modelo']:>9.1f} | "
            f"{r['rmse_a_persist']:>10.1f} | {r['rmse_a_tend']:>10.1f} | "
            f"{ganho:>+8.1f}%"
        )

    return melhor_nome, melhor_res


def _plotar_resultados(
    resultados: dict[str, dict[int, dict[str, Any]]],
    melhor_nome: str,
    melhor_res: dict[int, dict[str, Any]],
) -> None:
    """Gera e salva os gráficos de avaliação do estudo."""
    _, axes = plt.subplots(1, 3, figsize=(18, 5.5))
    ks = HORIZONTES

    ax0 = axes[0]
    for nome, res in resultados.items():
        rmse = [res[k]["rmse_modelo"] if k in res else np.nan for k in ks]
        ax0.plot(ks, rmse, marker="o", label=nome, linewidth=1.8)
    ax0.set_xlabel("Horizonte (h)")
    ax0.set_ylabel("RMSE (cm)")
    ax0.set_title("Todas as horas — nível")
    ax0.grid(True, alpha=0.3)
    ax0.legend(fontsize=8)

    ax1 = axes[1]
    for nome, res in resultados.items():
        rmse = [res[k]["rmse_a_modelo"] if k in res else np.nan for k in ks]
        ax1.plot(ks, rmse, marker="o", label=nome, linewidth=1.8)
    ax1.set_xlabel("Horizonte (h)")
    ax1.set_ylabel("RMSE (cm)")
    ax1.set_title(f"Ondas ativas — nível (ΔNE > {LIMIAR_DELTA_ATIVO_CM:.0f})")
    ax1.grid(True, alpha=0.3)
    ax1.legend(fontsize=8)

    ax2 = axes[2]
    r9 = melhor_res.get(9)
    if r9:
        n = min(24 * 30, len(r9["idx_te"]))
        ax2.plot(
            r9["idx_te"][-n:],
            r9["y_real"][-n:],
            color="black",
            linewidth=1.5,
            label="Real",
        )
        ax2.plot(
            r9["idx_te"][-n:],
            r9["y_pred"][-n:],
            color="seagreen",
            linewidth=1.5,
            alpha=0.85,
            label="Prev +9h v5",
        )
        ax2.set_xlabel("Data")
        ax2.set_ylabel("Nível T (cm)")
        ax2.set_title(f"Previsão +9h — {melhor_nome}")
        ax2.grid(True, alpha=0.3)
        ax2.legend()

    plt.suptitle(
        "Validação v5 — previsão de ΔT com OLS",
        fontsize=13,
        fontweight="bold",
    )
    plt.tight_layout(rect=[0, 0, 1, 0.96])
    plt.savefig("validacao_v5.png", dpi=150, bbox_inches="tight")
    plt.show()


def main() -> int:
    obter_supabase()
    log.info("=" * 90)
    log.info("VALIDAÇÃO v5 — PREVISÃO DE ΔT COM OLS")
    log.info("Split: treino ≤ %s / teste > %s", ANO_CORTE, ANO_CORTE)
    log.info(
        "OLS alpha: %s | Onda ativa: |ΔNE| > %s cm",
        OLS_ALPHA,
        LIMIAR_DELTA_ATIVO_CM,
    )
    log.info("=" * 90)

    df_full, df_safra = carregar_series()

    log.info("")
    log.info("DIAGNÓSTICO — DISTRIBUIÇÃO")
    for safra_ano in sorted(df_safra["safra"].unique()):
        sub = df_safra[df_safra["safra"] == safra_ano]
        n_total = len(sub)
        n_ativa = int((sub["delta_ne"].abs() > LIMIAR_DELTA_ATIVO_CM).sum())
        pct = 100.0 * n_ativa / n_total if n_total else 0.0
        log.info(
            "   Safra %s/%s | total=%5d | ativa=%3d (%.1f%%)",
            safra_ano,
            safra_ano + 1,
            n_total,
            n_ativa,
            pct,
        )

    resultados: dict[str, dict[int, dict[str, Any]]] = {}
    for nome, features in CONJUNTOS.items():
        log.info("%s", nome)
        resultados[nome] = {}
        for k in HORIZONTES:
            r = avaliar(df_full, df_safra, k, features)
            if r:
                resultados[nome][k] = r

    melhor_nome, melhor_res = _imprimir_tabelas(resultados)
    _plotar_resultados(resultados, melhor_nome, melhor_res)

    chaves_arrays = {
        "idx_te",
        "y_real",
        "y_pred",
        "delta_real",
        "delta_pred",
        "mask_ativo",
    }
    with open("metricas_v5.json", "w", encoding="utf-8") as f:
        json.dump(
            {
                nome: {
                    str(k): {
                        kk: vv
                        for kk, vv in r.items()
                        if kk not in chaves_arrays
                    }
                    for k, r in res.items()
                }
                for nome, res in resultados.items()
            },
            f,
            indent=2,
            default=str,
        )
    log.info("validacao_v5.png | metricas_v5.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
