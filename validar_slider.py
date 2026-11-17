import os
from datetime import datetime, timezone, timedelta
from core.config import configurar_logger
from core.db import obter_supabase

log = configurar_logger("teste_slider", formato_data="%H:%M:%S")

def registrar_e_validar():
    supabase = obter_supabase()
    
    # 1. Pega os dados atuais (nível e a previsão de 3h do slider)
    try:
        r_status = supabase.table("cache_site").select("dados").eq("chave", "status_atual").execute()
        r_onda = supabase.table("cache_site").select("dados").eq("chave", "onda_desfasada").execute()
        
        if not r_status.data or not r_onda.data:
            log.error("Dados insuficientes no cache_site.")
            return

        nivel_atual = float(r_status.data[0]["dados"]["nivel_atual"])
        previsao_3h = float(r_onda.data[0]["dados"].get("3", nivel_atual))
        
        # 2. Salva a nova previsão no banco
        supabase.table("teste_previsao").insert({
            "nivel_atual": nivel_atual,
            "nivel_previsto_3h": previsao_3h
        }).execute()
        log.info(f"Nova previsão registrada: Atual {nivel_atual}cm -> Previsto {previsao_3h}cm em 3h.")
        
    except Exception as e:
        log.error(f"Erro ao registrar previsão: {e}")

    # 3. Verifica se há previsões de 3 horas atrás para validar
    try:
        limite_tempo = datetime.now(timezone.utc) - timedelta(hours=3)
        
        # Busca previsões que ainda não foram verificadas e já passaram de 3h
        r_pendentes = supabase.table("teste_previsao") \
            .select("*") \
            .eq("verificado", False) \
            .lte("data_previsao", limite_tempo.isoformat()) \
            .execute()
            
        if not r_pendentes.data:
            log.info("Nenhuma previsão pendente de verificação no momento.")
            return

        for pendente in r_pendentes.data:
            id_teste = pendente["id"]
            previsto = float(pendente["nivel_previsto_3h"])
            
            # Pega o nível real que o rio está AGORA
            r_real = supabase.table("historico_ana") \
                .select("nivel_cm") \
                .eq("codigo_ana", "56696000") \
                .not_.is_("nivel_cm", "null") \
                .order("data_hora", desc=True) \
                .limit(1) \
                .execute()
                
            if r_real.data:
                real = float(r_real.data[0]["nivel_cm"])
                erro = round(previsto - real, 1)
                
                # Atualiza o banco com o resultado
                supabase.table("teste_previsao").update({
                    "nivel_real_3h": real,
                    "erro_cm": erro,
                    "verificado": True
                }).eq("id", id_teste).execute()
                
                log.info(f"✅ Previsão validada (ID {id_teste[:8]}): Previsto {previsto} | Real {real} | Erro {erro} cm")
                
    except Exception as e:
        log.error(f"Erro ao validar previsões antigas: {e}")

if __name__ == "__main__":
    registrar_e_validar()