import pandas as pd
import numpy as np
from supabase import create_client
import os
from dotenv import load_dotenv
from datetime import datetime, timedelta, timezone
from sklearn.linear_model import LinearRegression

load_dotenv()
supabase = create_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_KEY"))

data_limite = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()

print("Baixando dados (Paginando para não bater no limite do Supabase)...")

def baixar_tudo(tabela, colunas, codigo=None):
    todos_dados = []
    offset = 0
    limite = 1000
    while True:
        query = supabase.table(tabela).select(colunas).gte("data_hora", data_limite)
        if codigo:
            query = query.eq("codigo_ana", codigo)
        
        res = query.order("data_hora", desc=True).range(offset, offset + limite - 1).execute()
        
        if not res.data:
            break
        todos_dados.extend(res.data)
        if len(res.data) < limite:
            break
        offset += limite
    
    df = pd.DataFrame(todos_dados)
    if df.empty: return df
    
    # A CORREÇÃO ESTÁ AQUI: format='ISO8601' resolve o problema de datas com e sem milissegundos
    df["data_hora"] = pd.to_datetime(df["data_hora"], utc=True, format='ISO8601')
    return df

# 1. Nível e Chuva do Rio (ANA)
print("Baixando ANA...")
df_ana = baixar_tudo("historico_ana", "data_hora, nivel_cm, chuva_mm", "56696000")
df_ana = df_ana.dropna(subset=["nivel_cm"])
df_ana["data_hora"] = df_ana["data_hora"].dt.floor('h')
df_ana = df_ana.groupby("data_hora").agg({"nivel_cm": "mean", "chuva_mm": "sum"}).reset_index()

# 2. Defluência da Barragem (CEMIG)
print("Baixando CEMIG...")
df_cemig = baixar_tudo("historico_cemig", "data_hora, defluencia_m3s")
df_cemig = df_cemig.dropna(subset=["defluencia_m3s"])
df_cemig["data_hora"] = df_cemig["data_hora"].dt.floor('h')
df_cemig = df_cemig.groupby("data_hora").agg({"defluencia_m3s": "mean"}).reset_index()

print("\n--- DEBUG DE DATAS ---")
print(f"ANA vai de:   {df_ana['data_hora'].min()} até {df_ana['data_hora'].max()} (Total: {len(df_ana)})")
print(f"CEMIG vai de: {df_cemig['data_hora'].min()} até {df_cemig['data_hora'].max()} (Total: {len(df_cemig)})")
print("---------------------\n")

# Faz o merge exato
df = pd.merge(df_ana, df_cemig, on="data_hora", how="inner")

if df.empty:
    print("❌ ERRO: As datas não se sobrepõem.")
    exit()

df = df.set_index("data_hora").sort_index()
print(f"✅ Total de horas com dados sobrepostos: {len(df)}")

df["chuva_mm"] = df["chuva_mm"].fillna(0)

# Calcular as variações
df["delta_nivel"] = df["nivel_cm"].diff()
df["delta_vazao"] = df["defluencia_m3s"].diff()

print("\n--- TESTANDO DIFERENTES LAGS (TEMPO DE TRÂNSITO) ---")
melhor_lag = 0
melhor_corr = 0

for lag in range(1, 11):
    df["delta_vazao_passado"] = df["delta_vazao"].shift(lag)
    
    # Filtra apenas momentos de estiagem
    df_seco = df[df["chuva_mm"] < 1.0].dropna(subset=["delta_vazao_passado", "delta_nivel"])
    
    if len(df_seco) < 10: continue
    
    corr = df_seco["delta_vazao_passado"].corr(df_seco["delta_nivel"])
    print(f"Lag de {lag:02d}h: Correlação = {corr:.3f} (Baseado em {len(df_seco)} horas)")
    
    if corr > melhor_corr:
        melhor_corr = corr
        melhor_lag = lag

if melhor_corr > 0:
    print(f"\n🎯 O melhor tempo de trânsito foi de {melhor_lag} horas (Correlação: {melhor_corr:.3f})")
    
    df["delta_vazao_passado"] = df["delta_vazao"].shift(melhor_lag)
    df_seco = df[df["chuva_mm"] < 1.0].dropna(subset=["delta_vazao_passado", "delta_nivel"])
    
    X = df_seco[["delta_vazao_passado"]]
    y = df_seco["delta_nivel"]
    
    modelo = LinearRegression()
    modelo.fit(X, y)
    
    coef = modelo.coef_[0]
    print(f"Equação: Delta_Nivel = {coef:.4f} * Delta_Vazao")
    print(f"👉 Para cada +100 m³/s liberados pela barragem, o rio sobe {coef * 100:.1f} cm após {melhor_lag} horas.")
else:
    print("\nNão foi possível encontrar uma correlação válida.")