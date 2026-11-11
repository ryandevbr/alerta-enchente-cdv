import os
import re
import subprocess
import requests
from datetime import datetime, timezone
from core.config import configurar_logger
from core.db import obter_supabase, obter_ultima_leitura_ana, COD_TIMOTEO

log = configurar_logger("historico_visual", formato_data="%H:%M:%S")

# Cotas que queremos registrar na memória visual
COTAS_ALVO = [100, 150, 200, 250, 300, 400, 500, 620]

def baixar_ultimo_video():
    """Baixa o penúltimo vídeo da pasta de clipes do VPS."""
    url_listagem = "https://video.alertaenchentecdv.app/clipes/"
    try:
        r = requests.get(url_listagem, timeout=10)
        r.raise_for_status()
        arquivos = re.findall(r'href="([^"]+\.mp4)"', r.text)
        if len(arquivos) < 2:
            return False
        arquivo_alvo = sorted(arquivos)[-2]
        url_download = url_listagem + arquivo_alvo
        
        r_video = requests.get(url_download, stream=True)
        with open("video_temp.mp4", "wb") as f:
            for chunk in r_video.iter_content(chunk_size=8192):
                f.write(chunk)
        return True
    except Exception as e:
        log.error(f"Erro ao baixar vídeo: {e}")
        return False

def capturar_historico():
    supabase = obter_supabase()
    
    # 1. Descobre o nível atual
    leitura = obter_ultima_leitura_ana(COD_TIMOTEO, apenas_com_nivel=True)
    if not leitura:
        log.error("Sem dados do rio.")
        return
    nivel = float(leitura["nivel_cm"])
    log.info(f"Nível atual: {nivel} cm")

    # 2. Verifica em qual faixa de cota estamos (ex: se está em 215, a cota alvo é 200)
    cota_atual = max([c for c in COTAS_ALVO if c <= nivel], default=None)
    if not cota_atual:
        log.info("Nível abaixo da primeira cota de registro.")
        return

    # 3. Verifica se já temos essa foto no banco
    r_check = supabase.table("cache_site").select("dados").eq("chave", "historico_visual").execute()
    historico = r_check.data[0]["dados"] if r_check.data else {}
    
    if str(cota_atual) in historico:
        log.info(f"Já temos foto da cota {cota_atual} cm. Pulando.")
        return

    log.info(f"Nova cota atingida: {cota_atual} cm! Capturando foto...")

    # 4. Baixa o vídeo e extrai o frame
    if not baixar_ultimo_video():
        return

    try:
        # Extrai um frame de 1 segundo do vídeo
        subprocess.run([
            "ffmpeg", "-y", "-i", "video_temp.mp4", "-ss", "00:00:01", 
            "-vframes", "1", "frame_temp.jpg"
        ], check=True, capture_output=True)

        # 5. Faz upload para o Supabase
        with open("frame_temp.jpg", "rb") as f:
            nome_arquivo = f"cota_{cota_atual}cm.jpg"
            supabase.storage.from_("stories").upload(
                file=f.read(), path=nome_arquivo,
                file_options={"content-type": "image/jpeg", "upsert": "true"}
            )
            
        url_publica = supabase.storage.from_("stories").get_public_url(nome_arquivo)
        
        # 6. Salva no banco
        historico[str(cota_atual)] = url_publica
        
        supabase.table("cache_site").upsert({
            "chave": "historico_visual",
            "dados": historico,
            "atualizado_em": datetime.now(timezone.utc).isoformat()
        }, on_conflict="chave").execute()

        log.info(f"✅ Foto da cota {cota_atual} cm salva! URL: {url_publica}")

    except Exception as e:
        log.error(f"Erro ao extrair/salvar frame: {e}")

if __name__ == "__main__":
    capturar_historico()