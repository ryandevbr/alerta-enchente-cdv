# VALIDAÇÃO v5 — PREVISÃO DE ΔT COM OLS
# Alerta Enchente CDV

# Mudanças em relação ao v4:
#   Target é ΔT(t+k) = T(t+k) − T(t), não T(t+k)
#   OLS puro (Ridge α=0.01) em vez de Ridge α=1.0
#   Previsão final: ŷ = T(t) + ΔT_predito
#   Diagnóstico expandido com baseline "ΔT = 0"

# Autor: Ryan Lucas de Freitas Martins
# Licença: MIT


import os
import sys
import logging
import json
import pickle
from pathlib import Path
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd
import matplotlib.pyplot as plt
from dotenv import load_dotenv
from supabase import create_client, Client

from sklearn.linear_model import Ridge
from sklearn.preprocessing import StandardScaler
from sklearn.pipeline import Pipeline
from sklearn.metrics import mean_squared_error

# CONFIG

load_dotenv()
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
if not SUPABASE_URL or not SUPABASE_KEY:
    print("Faltam variáveis no .env"); sys.exit(1)

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
log = logging.getLogger("v5")

COD_NE       = "56661000"
COD_T        = "56696000"
COD_AD_NIVEL = "56675080"
COD_BARRA    = "56688080"

LAG_HORAS = 9
HORIZONTES = list(range(1, LAG_HORAS + 1))
SAFRAS = [2018, 2019, 2020, 2021, 2022, 2023, 2024, 2025]
ANO_CORTE = 2023

# OLS essencialmente — alpha quase zero
OLS_ALPHA = 0.01

LIMIAR_DELTA_ATIVO_CM = 40.0

# Cache local para acelerar re-execuções
CACHE_DIR = Path("cache_series")
CACHE_DIR.mkdir(exist_ok=True)

# DOWNLOAD COM CACHE

def _cache_path(codigo, desde, ate):
    return CACHE_DIR / f"{codigo}_{desde:%Y%m%d}_{ate:%Y%m%d}.pkl"


def carregar_estacao(codigo, desde, ate, page_size=1000, usar_cache=True):
    cache_file = _cache_path(codigo, desde, ate)

    if usar_cache and cache_file.exists():
        try:
            with open(cache_file, "rb") as f:
                df = pickle.load(f)
            log.info(f"   {codigo}: cache ({len(df):,} leituras)")
            return df
        except Exception:
            log.warning(f"   ⚠️ Cache corrompido para {codigo}. Baixando de novo.")

    todas, offset = [], 0
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
    log.info(f"   {codigo}: {len(df):,} leituras (cache salvo)")
    return df


# MONTAGEM DAS SÉRIES

def carregar_series():
    desde = datetime(SAFRAS[0], 12, 1, tzinfo=timezone.utc)
    ate   = datetime(SAFRAS[-1] + 1, 3, 1, tzinfo=timezone.utc)

    log.info(f"⬇️  Dados ({desde.date()} a {ate.date()})")
    df_ne = carregar_estacao(COD_NE, desde, ate)
    df_t  = carregar_estacao(COD_T,  desde, ate)
    df_ad = carregar_estacao(COD_AD_NIVEL, desde, ate)
    df_b  = carregar_estacao(COD_BARRA, desde, ate)

    log.info(f"   NE={len(df_ne):,} | T={len(df_t):,} | "
             f"AD={len(df_ad):,} | B={len(df_b):,}")

    if df_ne.empty or df_t.empty:
        log.error("NE ou T vazia."); sys.exit(1)
    if df_ad.empty:
        log.error("56675080 sem dados."); sys.exit(1)

    ne = (df_ne.resample("1h").last().ffill()
                .rename(columns={"nivel_cm": "ne"}))[["ne"]]
    t  = (df_t.resample("1h").last().ffill()
               .rename(columns={"nivel_cm": "t"}))[["t"]]
    ad = df_ad.resample("1h").last().ffill().rename(columns={
        "nivel_cm":  "ad_nivel",
        "vazao_m3s": "ad_vazao",
        "chuva_mm":  "ad_chuva",
    })[["ad_nivel", "ad_vazao", "ad_chuva"]]

    b = pd.DataFrame()
    if not df_b.empty:
        b = (df_b.resample("1h").last().ffill()
                  .rename(columns={"nivel_cm": "b_nivel"}))[["b_nivel"]]

    df = ne.join(t, how="inner").join(ad, how="left")
    if not b.empty:
        df = df.join(b, how="left")
    else:
        df["b_nivel"] = np.nan

    df = df.dropna(subset=["ne", "t"])

    # Shifts na série completa
    df["delta_ne"] = df["ne"] - df["ne"].shift(LAG_HORAS)
    df["delta_t"]  = df["t"]  - df["t"].shift(LAG_HORAS)
    df["delta_ad_nivel"] = df["ad_nivel"] - df["ad_nivel"].shift(LAG_HORAS)
    df["delta_ad_vazao"] = df["ad_vazao"] - df["ad_vazao"].shift(LAG_HORAS)

    for c in ["ad_nivel", "ad_vazao", "ad_chuva", "b_nivel"]:
        df[c] = df[c].ffill()

    df_full = df.copy()

    idx = df.index
    mask_wet = (((idx.month == 12) & (idx.day >= 15)) |
                (idx.month == 1) |
                ((idx.month == 2) & (idx.day <= 20)))
    df_safra = df[mask_wet].copy()
    df_safra["safra"] = np.where(df_safra.index.month == 12,
                                 df_safra.index.year,
                                 df_safra.index.year - 1)

    log.info(f"   Série completa: {len(df_full):,} h")
    log.info(f"   Safras:         {len(df_safra):,} h")
    return df_full, df_safra

# CONJUNTOS DE FEATURES

FEATURES_V3 = ["t", "ne", "delta_ne", "delta_t"]
CONJUNTOS = {
    "v3 (NE + T)":              FEATURES_V3,
    "v4a (+ AD nível)":         FEATURES_V3 + ["ad_nivel", "delta_ad_nivel"],
    "v4b (+ AD vazão)":         FEATURES_V3 + ["ad_vazao", "delta_ad_vazao"],
    "v4c (+ AD nível + vazão)": FEATURES_V3 + ["ad_nivel", "delta_ad_nivel",
                                                "ad_vazao", "delta_ad_vazao"],
    "v4d (+ v4c + barragem)":   FEATURES_V3 + ["ad_nivel", "delta_ad_nivel",
                                                "ad_vazao", "delta_ad_vazao",
                                                "b_nivel"],
}

# DATASET — AGORA COM TARGET = ΔT

def construir_dataset(df_full, df_safra, k, features):
    """
    Target: ΔT(t+k) = T(t+k) − T(t)
    A previsão final em nível será: ŷ(t+k) = T(t) + ΔT_predito
    """
    X = df_full[features].copy()
    y = (df_full["t"].shift(-k) - df_full["t"]).rename("y")

    dataset = pd.concat([X, y], axis=1).dropna()
    dataset = dataset.loc[dataset.index.intersection(df_safra.index)].copy()
    dataset["safra"] = df_safra.loc[dataset.index, "safra"]

    return dataset[features], dataset["y"].values, dataset.index, dataset["safra"].values

# AVALIAÇÃO

def avaliar(df_full, df_safra, k, features):
    X, y_delta, idx, safra = construir_dataset(df_full, df_safra, k, features)

    mask_tr = safra <= ANO_CORTE
    mask_te = safra >  ANO_CORTE

    X_tr, y_tr = X[mask_tr], y_delta[mask_tr]
    X_te, y_te_delta = X[mask_te], y_delta[mask_te]

    if len(X_tr) < 100 or len(X_te) < 50:
        return None

    # OLS puro
    modelo = Pipeline([
        ("scaler", StandardScaler()),
        ("ols", Ridge(alpha=OLS_ALPHA)),
    ])
    modelo.fit(X_tr, y_tr)

    # Previsão: T(t) + ΔT_predito
    delta_pred = modelo.predict(X_te)
    t_atual = X_te["t"].values
    y_pred_nivel = t_atual + delta_pred

    # Nível real (reconstruído do delta)
    y_real_nivel = t_atual + y_te_delta

    # Baselines em nível
    y_persist = t_atual                                    # ΔT = 0
    taxa_t = X_te["delta_t"].values / LAG_HORAS
    y_tend = t_atual + taxa_t * k                          # tendência linear

    # ---- RMSE em NÍVEL ----
    rmse_m = float(np.sqrt(mean_squared_error(y_real_nivel, y_pred_nivel)))
    rmse_p = float(np.sqrt(mean_squared_error(y_real_nivel, y_persist)))
    rmse_t = float(np.sqrt(mean_squared_error(y_real_nivel, y_tend)))

    # ---- RMSE em DELTA (diagnóstico extra) ----
    rmse_delta_m = float(np.sqrt(mean_squared_error(y_te_delta, delta_pred)))
    rmse_delta_p = float(np.sqrt(mean_squared_error(y_te_delta,
                                                     np.zeros_like(y_te_delta))))
    rmse_delta_t = float(np.sqrt(mean_squared_error(y_te_delta, taxa_t * k)))

    # ---- Ondas ativas ----
    mask_ativo = np.abs(X_te["delta_ne"].values) > LIMIAR_DELTA_ATIVO_CM
    if mask_ativo.sum() >= 20:
        rmse_am = float(np.sqrt(mean_squared_error(
            y_real_nivel[mask_ativo], y_pred_nivel[mask_ativo])))
        rmse_ap = float(np.sqrt(mean_squared_error(
            y_real_nivel[mask_ativo], y_persist[mask_ativo])))
        rmse_at = float(np.sqrt(mean_squared_error(
            y_real_nivel[mask_ativo], y_tend[mask_ativo])))
        rmse_delta_am = float(np.sqrt(mean_squared_error(
            y_te_delta[mask_ativo], delta_pred[mask_ativo])))
        rmse_delta_ap = float(np.sqrt(mean_squared_error(
            y_te_delta[mask_ativo], np.zeros_like(y_te_delta[mask_ativo]))))
        rmse_delta_at = float(np.sqrt(mean_squared_error(
            y_te_delta[mask_ativo], (taxa_t * k)[mask_ativo])))
    else:
        rmse_am = rmse_ap = rmse_at = np.nan
        rmse_delta_am = rmse_delta_ap = rmse_delta_at = np.nan

    return {
        "k": k,
        "n_treino": int(mask_tr.sum()),
        "n_teste": int(mask_te.sum()),
        "n_ativo": int(mask_ativo.sum()),
        "rmse_modelo": rmse_m, "rmse_persist": rmse_p, "rmse_tend": rmse_t,
        "rmse_a_modelo": rmse_am, "rmse_a_persist": rmse_ap, "rmse_a_tend": rmse_at,
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

# MAIN

def main():
    log.info("=" * 90)
    log.info("VALIDAÇÃO v5 — PREVISÃO DE ΔT COM OLS")
    log.info(f"Split: treino ≤ {ANO_CORTE} / teste > {ANO_CORTE}")
    log.info(f"OLS alpha: {OLS_ALPHA} | Onda ativa: |ΔNE| > {LIMIAR_DELTA_ATIVO_CM} cm")
    log.info("=" * 90)

    df_full, df_safra = carregar_series()

    log.info("")
    log.info("DIAGNÓSTICO — DISTRIBUIÇÃO")
    for safra_ano in sorted(df_safra["safra"].unique()):
        sub = df_safra[df_safra["safra"] == safra_ano]
        n_total = len(sub)
        n_ativa = int((sub["delta_ne"].abs() > 40).sum())
        pct = 100 * n_ativa / n_total if n_total else 0
        log.info(f"   Safra {safra_ano}/{safra_ano+1} | total={n_total:>5} | "
                 f"ativa={n_ativa:>3} ({pct:.1f}%)")

    resultados = {}
    for nome, features in CONJUNTOS.items():
        log.info(f"{nome}")
        resultados[nome] = {}
        for k in HORIZONTES:
            r = avaliar(df_full, df_safra, k, features)
            if r:
                resultados[nome][k] = r

    # ---- Tabela 1: +9h em NÍVEL (todas as horas) ----
    print("\n" + "=" * 100)
    print("COMPARAÇÃO EM +9h — TESTE TEMPORAL (todas as horas)")
    print("=" * 100)
    print(f"{'Conjunto':<30} | {'RMSE mod':>9} | {'RMSE pers':>10} | "
          f"{'RMSE tend':>10} | {'ganho':>9}")
    print("-" * 100)
    for nome, res in resultados.items():
        r = res.get(9)
        if not r: continue
        mb = min(r["rmse_persist"], r["rmse_tend"])
        ganho = (1 - r["rmse_modelo"] / mb) * 100
        print(f"{nome:<30} | {r['rmse_modelo']:>9.1f} | "
              f"{r['rmse_persist']:>10.1f} | {r['rmse_tend']:>10.1f} | "
              f"{ganho:>+8.1f}%")

    # ---- Tabela 2: +9h em NÍVEL (ondas ativas) ----
    print("\n" + "=" * 100)
    print(f"COMPARAÇÃO EM +9h — ONDAS ATIVAS (NÍVEL, |ΔNE| > {LIMIAR_DELTA_ATIVO_CM} cm)")
    print("=" * 100)
    print(f"{'Conjunto':<30} | {'n ativo':>8} | {'RMSE mod':>9} | "
          f"{'RMSE pers':>10} | {'RMSE tend':>10} | {'ganho':>9}")
    print("-" * 100)
    for nome, res in resultados.items():
        r = res.get(9)
        if not r or np.isnan(r["rmse_a_modelo"]): continue
        mb = min(r["rmse_a_persist"], r["rmse_a_tend"])
        ganho = (1 - r["rmse_a_modelo"] / mb) * 100
        print(f"{nome:<30} | {r['n_ativo']:>8} | {r['rmse_a_modelo']:>9.1f} | "
              f"{r['rmse_a_persist']:>10.1f} | {r['rmse_a_tend']:>10.1f} | "
              f"{ganho:>+8.1f}%")

    # ---- Tabela 3: +9h em ΔT (diagnóstico) ----
    print("\n" + "=" * 100)
    print(f"COMPARAÇÃO EM +9h — ONDAS ATIVAS (ΔT, |ΔNE| > {LIMIAR_DELTA_ATIVO_CM} cm)")
    print("=" * 100)
    print(f"{'Conjunto':<30} | {'RMSE ΔT mod':>12} | {'RMSE ΔT pers':>13} | "
          f"{'RMSE ΔT tend':>13} | {'ganho':>9}")
    print("-" * 100)
    for nome, res in resultados.items():
        r = res.get(9)
        if not r or np.isnan(r["rmse_delta_a_modelo"]): continue
        mb = min(r["rmse_delta_a_persist"], r["rmse_delta_a_tend"])
        ganho = (1 - r["rmse_delta_a_modelo"] / mb) * 100
        print(f"{nome:<30} | {r['rmse_delta_a_modelo']:>12.1f} | "
              f"{r['rmse_delta_a_persist']:>13.1f} | "
              f"{r['rmse_delta_a_tend']:>13.1f} | {ganho:>+8.1f}%")

    # ---- Melhor conjunto ----
    def chave(item):
        nome, res = item
        r = res.get(9)
        if not r or np.isnan(r["rmse_a_modelo"]): return float("inf")
        return r["rmse_a_modelo"]

    melhor_nome, melhor_res = min(resultados.items(), key=chave)

    print("\n" + "=" * 100)
    print(f"MELHOR CONJUNTO: {melhor_nome}")
    print("=" * 100)
    print(f"\n{'k':>3} | {'RMSE mod':>9} | {'RMSE pers':>10} | "
          f"{'RMSE tend':>10} | {'ganho':>9}")
    print("-" * 60)
    for k in HORIZONTES:
        r = melhor_res.get(k)
        if not r: continue
        mb = min(r["rmse_persist"], r["rmse_tend"])
        ganho = (1 - r["rmse_modelo"] / mb) * 100
        print(f"{k:>3} | {r['rmse_modelo']:>9.1f} | {r['rmse_persist']:>10.1f} | "
              f"{r['rmse_tend']:>10.1f} | {ganho:>+8.1f}%")

    print(f"\n[Ondas ativas]")
    print(f"{'k':>3} | {'n ativo':>8} | {'RMSE mod':>9} | "
          f"{'RMSE pers':>10} | {'RMSE tend':>10} | {'ganho':>9}")
    print("-" * 90)
    for k in HORIZONTES:
        r = melhor_res.get(k)
        if not r or np.isnan(r["rmse_a_modelo"]): continue
        mb = min(r["rmse_a_persist"], r["rmse_a_tend"])
        ganho = (1 - r["rmse_a_modelo"] / mb) * 100
        print(f"{k:>3} | {r['n_ativo']:>8} | {r['rmse_a_modelo']:>9.1f} | "
              f"{r['rmse_a_persist']:>10.1f} | {r['rmse_a_tend']:>10.1f} | "
              f"{ganho:>+8.1f}%")

    # ---- Gráficos ----
    fig, axes = plt.subplots(1, 3, figsize=(18, 5.5))
    ks = HORIZONTES

    ax = axes[0]
    for nome, res in resultados.items():
        rmse = [res[k]["rmse_modelo"] if k in res else np.nan for k in ks]
        ax.plot(ks, rmse, marker="o", label=nome, linewidth=1.8)
    ax.set_xlabel("Horizonte (h)"); ax.set_ylabel("RMSE (cm)")
    ax.set_title("Todas as horas — nível")
    ax.grid(True, alpha=0.3); ax.legend(fontsize=8)

    ax = axes[1]
    for nome, res in resultados.items():
        rmse = [res[k]["rmse_a_modelo"] if k in res else np.nan for k in ks]
        ax.plot(ks, rmse, marker="o", label=nome, linewidth=1.8)
    ax.set_xlabel("Horizonte (h)"); ax.set_ylabel("RMSE (cm)")
    ax.set_title(f"Ondas ativas — nível (ΔNE > {LIMIAR_DELTA_ATIVO_CM:.0f})")
    ax.grid(True, alpha=0.3); ax.legend(fontsize=8)

    ax = axes[2]
    r9 = melhor_res.get(9)
    if r9:
        n = min(24 * 30, len(r9["idx_te"]))
        ax.plot(r9["idx_te"][-n:], r9["y_real"][-n:],
                color="black", linewidth=1.5, label="Real")
        ax.plot(r9["idx_te"][-n:], r9["y_pred"][-n:],
                color="seagreen", linewidth=1.5, alpha=0.85, label="Prev +9h v5")
        ax.set_xlabel("Data"); ax.set_ylabel("Nível T (cm)")
        ax.set_title(f"Previsão +9h — {melhor_nome}")
        ax.grid(True, alpha=0.3); ax.legend()

    plt.suptitle("Validação v5 — previsão de ΔT com OLS",
                 fontsize=13, fontweight="bold")
    plt.tight_layout(rect=[0, 0, 1, 0.96])
    plt.savefig("validacao_v5.png", dpi=150, bbox_inches="tight")
    plt.show()

    with open("metricas_v5.json", "w", encoding="utf-8") as f:
        json.dump({
            nome: {str(k): {kk: vv for kk, vv in r.items()
                            if kk not in ("idx_te", "y_real", "y_pred",
                                          "delta_real", "delta_pred", "mask_ativo")}
                   for k, r in res.items()}
            for nome, res in resultados.items()
        }, f, indent=2, default=str)
    log.info("validacao_v5.png | metricas_v5.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
