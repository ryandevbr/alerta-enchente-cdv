import logging
from datetime import datetime

#1. MOTOR PREDITIVO (Velocidade e Tendência)

def calcular_velocidade_suavizada(leituras_supabase: list) -> dict:
    """
    Recebe as últimas 4 a 5 leituras (aprox. 1 hora) do Supabase.
    Aplica filtro de ruído e calcula a derivada temporal.
    """
    if len(leituras_supabase) < 2:
        return {"velocidade": 0.0, "tendencia": "ESTÁVEL", "confiavel": False}

    # 1. Filtro Anti-Ruído (Outlier Rejection)
    leituras_validas = []
    for i in range(len(leituras_supabase) - 1):
        atual = leituras_supabase[i]['nivel_cm']
        anterior = leituras_supabase[i+1]['nivel_cm']
        
        # O rio não sobe/desce mais de 50cm em apenas 15 minutos de forma natural.
        # Se isso ocorrer, é detrito batendo no sensor da ANA. Descartamos o salto.
        if abs(atual - anterior) > 50:
            logging.warning(f"Ruído de sensor ignorado: salto de {anterior} para {atual}")
            continue
        leituras_validas.append(leituras_supabase[i])
    
    if leituras_supabase:
         leituras_validas.append(leituras_supabase[-1]) # Garante o último item

    if len(leituras_validas) < 2:
         return {"velocidade": 0.0, "tendencia": "ESTÁVEL", "confiavel": False}

    # 2. Cálculo da Derivada Suavizada (Delta N / Delta t)
    recente = leituras_validas[0]
    antiga = leituras_validas[-1] 
    
    t1 = datetime.fromisoformat(recente['data_hora'].replace("Z", "+00:00"))
    t0 = datetime.fromisoformat(antiga['data_hora'].replace("Z", "+00:00"))
    
    delta_t_horas = (t1 - t0).total_seconds() / 3600.0

    if delta_t_horas <= 0.16: # Evita divisões por tempos menores que 10 minutos
        return {"velocidade": 0.0, "tendencia": "ESTÁVEL", "confiavel": False}

    # Aplicação da fórmula
    delta_n_cm = recente['nivel_cm'] - antiga['nivel_cm']
    velocidade_cm_h = delta_n_cm / delta_t_horas

    # 3. Classificação Lexical Segura
    tendencia = "ESTÁVEL"
    if velocidade_cm_h > 2.0:
        tendencia = "SUBINDO"
    elif velocidade_cm_h < -2.0:
        tendencia = "BAIXANDO"

    return {
        "velocidade": round(velocidade_cm_h, 1),
        "tendencia": tendencia,
        "confiavel": True
    }


#2. MOTOR DE IMPACTO HIPERLOCAL (Ocupação)

def calcular_ocupacao_calha(nivel_atual: float, ruas_banco: list) -> list:
    """
    Traduz a cota hidrológica para o impacto percentual em cada rua.
    """
    relatorio_ruas = []
    
    for rua in ruas_banco:
        cota_critica = rua['nivel_critico_cm']
        
        # Aplicação da fórmula de Ocupação da Calha
        ocupacao_pct = (nivel_atual / cota_critica) * 100
        
        relatorio_ruas.append({
            "nome": rua['nome'],
            "bairro": rua['bairro'],
            "ocupacao_pct": round(ocupacao_pct, 1),
            "critico": ocupacao_pct >= 100.0,
            "alerta": 80.0 <= ocupacao_pct < 100.0
        })
        
    #Retorna a lista ordenada: as ruas em maior perigo aparecem primeiro
    return sorted(relatorio_ruas, key=lambda x: x['ocupacao_pct'], reverse=True)