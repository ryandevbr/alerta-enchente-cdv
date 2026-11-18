import os
import io
import re
import sys
import subprocess
import requests
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from PIL import Image, ImageDraw, ImageFont
from core.config import configurar_logger
from core.db import obter_supabase

log = configurar_logger("story", formato_data="%H:%M:%S")

FONTE_BOLD = "assets/OpenSans-Bold.ttf"
FONTE_REGULAR = "assets/OpenSans-Regular.ttf"

# Coordenadas (validadas)
POS_NIVEL = (540, 950)       
POS_HOJE = (450, 1255)       
POS_ANO1 = (750, 1255)       
POS_ANO2 = (450, 1340)       
POS_ANO3 = (750, 1340)       
POS_DEFLUENCIA = (580, 1520) 
POS_DATA_HORA = (580, 1800)

def obter_dados(supabase):
    try:
        r_status = supabase.table("cache_site").select("dados").eq("chave", "status_atual").execute()
        dados_status = r_status.data[0]["dados"]
        nivel = float(dados_status["nivel_atual"])
        defluencia = float(dados_status.get("defluencia_cemig") or 0.0)
        
        r_comp = supabase.table("cache_site").select("dados").eq("chave", "comparativo_anual").execute()
        comp = r_comp.data[0]["dados"]
        anos = {a["ano"]: a["nivel_cm"] for a in comp.get("anos_anteriores", [])}
        
        return nivel, defluencia, anos
    except Exception as e:
        log.error(f"Erro ao buscar dados no Supabase: {e}")
        return None, None, None

def obter_anos_comparacao():
    if datetime.now().year == 2026:
        return [2023, 2021, 2018]
    return [2024, 2022, 2019]

def gerar_overlay(nivel, defluencia, anos):
    """Gera a imagem PNG transparente com os textos."""
    if nivel < 450: template_path = "template_video_normal.png"
    elif nivel < 540: template_path = "template_video_atencao.png"
    elif nivel < 620: template_path = "template_video_alerta.png"
    else: template_path = "template_video_inundacao.png"

    img = Image.open(template_path).convert("RGBA")
    draw = ImageDraw.Draw(img)
    
    font_nivel = ImageFont.truetype(FONTE_BOLD, 120)
    font_anos = ImageFont.truetype(FONTE_BOLD, 35)
    font_cemig = ImageFont.truetype(FONTE_BOLD, 40)
    font_data = ImageFont.truetype(FONTE_REGULAR, 30)

    draw.text(POS_NIVEL, f"{nivel:.0f} cm", font=font_nivel, fill="white", anchor="mm")
    draw.text(POS_HOJE, f"HOJE: {nivel:.0f}cm", font=font_anos, fill="white", anchor="mm")
    
    anos_alvo = obter_anos_comparacao()
    for i, pos in enumerate([POS_ANO1, POS_ANO2, POS_ANO3]):
        ano = anos_alvo[i]
        valor = anos.get(ano, 0)
        draw.text(pos, f"{ano}: {valor:.0f}cm", font=font_anos, fill="white", anchor="mm")

    texto_def = f"Defluência: {defluencia:.0f} m³/s" if defluencia > 0 else "Defluência: --"
    draw.text(POS_DEFLUENCIA, texto_def, font=font_cemig, fill="white", anchor="mm")

    agora = datetime.now(ZoneInfo("America/Sao_Paulo"))
    draw.text(POS_DATA_HORA, agora.strftime("%d/%m/%Y às %H:%M"), font=font_data, fill="#cbd5e1", anchor="mm")

    return img

def baixar_ultimo_video():
    """Baixa o penúltimo vídeo da pasta de clipes do VPS para garantir que está completo."""
    url_listagem = "https://video.alertaenchentecdv.app/clipes/"
    try:
        r = requests.get(url_listagem, timeout=10)
        r.raise_for_status()
        arquivos = re.findall(r'href="([^"]+\.mp4)"', r.text)
        if len(arquivos) < 2: return False
        arquivo_alvo = sorted(arquivos)[-2]
        
        r_video = requests.get(url_listagem + arquivo_alvo, stream=True)
        with open("video_temp.mp4", "wb") as f:
            for chunk in r_video.iter_content(chunk_size=8192):
                f.write(chunk)
        return True
    except Exception as e:
        log.error(f"Erro ao baixar vídeo: {e}")
        return False

def publicar(modo="video"):
    supabase = obter_supabase()
    nivel, defluencia, anos = obter_dados(supabase)
    if nivel is None: return

    img_overlay = gerar_overlay(nivel, defluencia, anos)

    if modo == "imagem":
        log.info("Gerando Story em formato de IMAGEM...")
        img_byte_arr = io.BytesIO()
        img_overlay.convert("RGB").save(img_byte_arr, format='JPEG', quality=95)
        img_byte_arr.seek(0)
        
        nome_arquivo = "story_estatico.jpg"
        supabase.storage.from_("stories").upload(
            file=img_byte_arr.read(), path=nome_arquivo,
            file_options={"content-type": "image/jpeg", "upsert": "true"}
        )
        url_final = supabase.storage.from_("stories").get_public_url(nome_arquivo)
        msg_telegram = "✅ Story (Imagem) gerado e pronto para postar!"
        
    else:
        log.info("Gerando Story em formato de VÍDEO...")
        img_overlay.save("overlay_temp.png")
        
        if not baixar_ultimo_video():
            log.error("Falha ao baixar vídeo do VPS. Abortando.")
            return

        comando = [
            "ffmpeg", "-y", "-i", "video_temp.mp4", "-i", "overlay_temp.png",
            "-filter_complex", "[0:v]scale=1080:-2,pad=1080:1920:0:0:color=black[vid];[vid][1:v]overlay=0:0",
            "-c:v", "libx264", "-preset", "fast", "-crf", "23", "-an", "story_final.mp4"
        ]
        subprocess.run(comando, check=True)

        with open("story_final.mp4", "rb") as f:
            nome_arquivo = "story_video.mp4"
            supabase.storage.from_("stories").upload(
                file=f.read(), path=nome_arquivo,
                file_options={"content-type": "video/mp4", "upsert": "true"}
            )
        url_final = supabase.storage.from_("stories").get_public_url(nome_arquivo)
        msg_telegram = "✅ Story (Vídeo) gerado e pronto para postar!"

    # Salva no banco
    supabase.table("cache_site").upsert({
        "chave": "story_atual",
        "dados": {"url": url_final, "gerado_em": datetime.now(timezone.utc).isoformat(), "tipo": modo},
        "atualizado_em": datetime.now(timezone.utc).isoformat()
    }, on_conflict="chave").execute()

    log.info(f"✅ Concluído! URL: {url_final}")

    # Avisa o Telegram
    TOKEN = os.getenv("TELEGRAM_TOKEN")
    CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
    if TOKEN and CHAT_ID:
        requests.post(f"https://api.telegram.org/bot{TOKEN}/sendMessage", json={
            "chat_id": CHAT_ID, "text": msg_telegram
        })

if __name__ == "__main__":
    # Verifica se passou o argumento. Se não, assume 'video'
    modo_escolhido = sys.argv[1] if len(sys.argv) > 1 else "video"
    publicar(modo_escolhido)