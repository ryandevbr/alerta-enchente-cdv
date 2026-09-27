import requests
import pandas as pd
import matplotlib.pyplot as plt

# 1. CONFIGURAÇÕES DA API OPEN-METEO
# Coordenadas geográficas focadas em Timóteo / Cachoeira do Vale
LATITUDE = -19.5314
LONGITUDE = -42.6458

def buscar_previsao_chuva():
    print("Conectando aos satélites da Open-Meteo...")
    
    url = "https://api.open-meteo.com/v1/forecast"
    parametros = {
        "latitude": LATITUDE,
        "longitude": LONGITUDE,
        "hourly": "precipitation",        # Queremos apenas a chuva (em mm)
        "timezone": "America/Sao_Paulo",  # Fuso horário local para alinhar com nosso banco
        "forecast_days": 3                # Vamos buscar as próximas 72 horas
    }
    
    resposta = requests.get(url, params=parametros)
    
    if resposta.status_code == 200:
        dados = resposta.json()
        
        # Extrai as listas de datas e de chuvas do JSON
        datas = dados['hourly']['time']
        chuvas = dados['hourly']['precipitation']
        
        # Converte para um DataFrame do Pandas (padrão que o Prophet entende)
        df_clima = pd.DataFrame({
            'data_hora': pd.to_datetime(datas),
            'chuva_previsao_mm': chuvas
        })
        
        print("Previsão de 72 horas extraída com sucesso!")
        return df_clima
    else:
        print(f"Erro ao buscar clima: {resposta.status_code}")
        return None

# 2. TESTE E VISUALIZAÇÃO

if __name__ == "__main__":
    df_futuro = buscar_previsao_chuva()
    
    if df_futuro is not None:
        # Exibe as primeiras 10 horas no terminal
        print("\nPrévia das próximas horas:")
        print(df_futuro.head(10).to_string(index=False))
        
        # Soma total de chuva esperada nos próximos 3 dias
        chuva_total = df_futuro['chuva_previsao_mm'].sum()
        print(f"\nVolume total de chuva esperado para 72h: {chuva_total:.1f} mm")
        
        # Plota um gráfico rápido para vermos o comportamento da chuva
        plt.figure(figsize=(10, 4))
        plt.plot(df_futuro['data_hora'], df_futuro['chuva_previsao_mm'], color='blue', marker='o')
        plt.title('Previsão de Chuva - Timóteo (Próximas 72h)')
        plt.xlabel('Data e Hora')
        plt.ylabel('Chuva (mm/h)')
        plt.grid(True)
        plt.tight_layout()
        plt.show()