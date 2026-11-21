import os
import io
import re
import sys
import subprocess
import requests
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
from PIL import Image, ImageDraw, ImageFont
from core.config import configurar_logger
from core.db import obter_supabase

log = configurar_logger("story", formato_data="%H:%M:%S")

FONTE_BOLD = "assets/OpenSans-Bold.ttf"
FONTE_REGULAR = "assets/OpenSans-Regular.ttf"

# =========================================================
# COORDENADAS PARA O TEMPLATE DE IMAGEM (ESTÁTICO)
# =========================================================
POS_IMG_NIVEL = (540, 700)
POS_IMG_TENDENCIA = (540, 1040)
POS_IMG_HOJE = (450, 1255)       
POS_IMG_ANO1 = (750, 1255)       
POS_IMG_ANO2 = (450, 1340)       
POS_IMG_ANO3 = (750, 1340)       
POS_IMG_DEFLUENCIA = (580, 1520) 
POS_IMG_DATA_HORA = (580, 1800)

# =========================================================
# COORDENADAS PARA O TEMPLATE DE VÍDEO (TRANSPARENTE NO TOPO)
# =========================================================
POS_VID_NIVEL = (540, 950)
POS_VID_TENDENCIA = (540, 1080) # Ajuste aqui quando colocar o campo no Canva
POS_VID_HOJE = (450, 1255)       
POS_VID_ANO1 = (750, 1255)       
POS_VID_ANO2 = (450, 1340)       
POS_VID_ANO3 = (750, 1340)       
POS_VID_DEFLUENCIA = (580, 1520) 
POS_VID_DATA_HORA = (580, 1800)


def obter_dados(supabase):
    try:
        r_status = supabase.table("cache_site").select("dados").eq("chave", "status_atual").execute()
        dados_status = r_status.data[0]["dados"]
        nivel = float(dados_status["nivel_atual"])
        defluencia = float(dados_status.get("defluencia_cemig") or 0.0)
        
        r_comp = supabase.table("cache_site").select("dados").eq("chave", "comparativo_anual").execute()
        comp = r_comp.data[0]["dados"]
        anos = {a["ano"]: a["nivel_cm"] for a in comp.get("anos_anteriores", [])}
        
        limite_12h = (datetime.now(timezone.utc) - timedelta(hours=12)).isoformat()
        r_12h = supabase.table("historico_ana") \
            .select("nivel_cm") \
            .eq("codigo_ana", "56696000") \
            .lte("data_hora", limite_12h) \
            .order("data_hora", desc=True) \
            .limit(1) \
            .execute()
        
        tendencia = None
        if r_12h.data:
            nivel_12h = float(r_12h.data[0]["nivel_cm"])
            tendencia = round(nivel - nivel_12h, 1)
            
        return nivel, defluencia, anos, tendencia
    except Exception as e:
        log.error(f"Erro ao buscar dados no Supabase: {e}")
        return None, None, None, None

def obter_anos_comparacao():
    if datetime.now().year == 2026:
        return [2023, 2021, 2018]
    return [2024, 2022, 2019]

def gerar_overlay(nivel, defluencia, anos, tendencia, modo="video"):
    # Define o template e as coordenadas com base no modo
    if modo == "imagem":
        prefixo = "template_"
        pos_nivel = POS_IMG_NIVEL
        pos_tendencia = POS_IMG_TENDENCIA
        pos_hoje = POS_IMG_HOJE
        pos_ano1 = POS_IMG_ANO1
        pos_ano2 = POS_IMG_ANO2
        pos_ano3 = POS_IMG_ANO3
        pos_defluencia = POS_IMG_DEFLUENCIA
        pos_data = POS_IMG_DATA_HORA
    else:
        prefixo = "template_video_"
        pos_nivel = POS_VID_NIVEL
        pos_tendencia = POS_VID_TENDENCIA
        pos_hoje = POS_VID_HOJE
        pos_ano1 = POS_VID_ANO1
        pos_ano2 = POS_VID_ANO2
        pos_ano3 = POS_VID_ANO3
        pos_defluencia = POS_VID_DEFLUENCIA
        pos_data = POS_VID_DATA_HORA

    if nivel < 450: template_path = f"{prefixo}normal.png"
    elif nivel < 540: template_path = f"{prefixo}atencao.png"
    elif nivel < 620: template_path = f"{prefixo}alerta.png"
    else: template_path = f"{prefixo}inundacao.png"

    if not os.path.exists(template_path):
        log.error(f"Arquivo {template_path} não encontrado!")
        return None

    img = Image.open(template_path).convert("RGBA")
    draw = ImageDraw.Draw(img)
    
    font_nivel = ImageFont.truetype(FONTE_BOLD, 120)
    font_tendencia = ImageFont.truetype(FONTE_BOLD, 35)
    font_anos = ImageFont.truetype(FONTE_BOLD, 35)
    font_cemig = ImageFont.truetype(FONTE_BOLD, 40)
    font_data = ImageFont.truetype(FONTE_REGULAR, 30)

    # 1. Nível grande
    draw.text(pos_nivel, f"{nivel:.0f} cm", font=font_nivel, fill="white", anchor="mm")
    
    # 2. Tendência de 12h
    if tendencia is not None:
        if tendencia > 2:
            texto_tend = f"Subiu {tendencia:.0f} cm nas ultimas 12h"
            cor_tend = "#F96363"
        elif tendencia < -2:
            texto_tend = f"Desceu {abs(tendencia):.0f} cm nas ultimas 12h"
            cor_tend = "#6DFF99"
        else:
            texto_tend = "Estavel nas ultimas 12h"
            cor_tend = "#ffffff"
            
        draw.text(pos_tendencia, texto_tend, font=font_tendencia, fill=cor_tend, anchor="mm")

    # 3. Comparativo
    draw.text(pos_hoje, f"HOJE: {nivel:.0f}cm", font=font_anos, fill="white", anchor="mm")
    anos_alvo = obter_anos_comparacao()
    for i, pos in enumerate([pos_ano1, pos_ano2, pos_ano3]):
        ano = anos_alvo[i]
        valor = anos.get(ano) # Pega o valor, se não existir, retorna None
        
        if valor is not None:
            texto_ano = f"{ano}: {valor:.0f}cm"
        else:
            texto_ano = f"{ano}: --" # Se não tem dado, mostra tracinhos
            
        draw.text(pos, texto_ano, font=font_anos, fill="white", anchor="mm")

    # 4. Defluência e Data
    texto_def = f"Defluencia: {defluencia:.0f} m3/s" if defluencia > 0 else "Defluencia: --"
    draw.text(pos_defluencia, texto_def, font=font_cemig, fill="white", anchor="mm")

    agora = datetime.now(ZoneInfo("America/Sao_Paulo"))
    draw.text(pos_data, agora.strftime("%d/%m/%Y as %H:%M"), font=font_data, fill="#cbd5e1", anchor="mm")

    return img

def baixar_ultimo_video():
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
        log.error(f"Erro ao baixar video: {e}")
        return False

def publicar(modo="video"):
    supabase = obter_supabase()
    nivel, defluencia, anos, tendencia = obter_dados(supabase)
    if nivel is None: return

    # Passa o 'modo' para a função gerar_overlay
    img_overlay = gerar_overlay(nivel, defluencia, anos, tendencia, modo)

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
        msg_telegram = "Story (Imagem) gerado e pronto para postar!"
        
    else:
        log.info("Gerando Story em formato de VIDEO...")
        img_overlay.save("overlay_temp.png")
        
        if not baixar_ultimo_video():
            log.error("Falha ao baixar video do VPS. Abortando.")
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
        msg_telegram = "Story (Video) gerado e pronto para postar!"

    supabase.table("cache_site").upsert({
        "chave": "story_atual",
        "dados": {"url": url_final, "gerado_em": datetime.now(timezone.utc).isoformat(), "tipo": modo},
        "atualizado_em": datetime.now(timezone.utc).isoformat()
    }, on_conflict="chave").execute()

    log.info(f"Concluido! URL: {url_final}")

    TOKEN = os.getenv("TELEGRAM_TOKEN")
    CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
    if TOKEN and CHAT_ID:
        requests.post(f"https://api.telegram.org/bot{TOKEN}/sendMessage", json={
            "chat_id": CHAT_ID, "text": msg_telegram
        })

def executar_teste():
    log.info("=== MODO DE TESTE ATIVADO ===")
    anos_ficticios = {2023: 450, 2021: 380, 2018: 520}
    
    # 1. Teste do template de Imagem
    img_estatica = gerar_overlay(nivel=300, defluencia=85, anos=anos_ficticios, tendencia=15, modo="imagem")
    if img_estatica:
        img_estatica.convert("RGB").save("teste_layout_imagem.jpg", quality=95)
        log.info("Gerado: teste_layout_imagem.jpg")
        
    # 2. Teste do template de Vídeo
    img_video = gerar_overlay(nivel=300, defluencia=85, anos=anos_ficticios, tendencia=15, modo="video")
    if img_video:
        # Como o template de vídeo tem o topo transparente, colocamos um fundo preto
        # para simular como ficará quando o FFmpeg colocar o vídeo do rio atrás.
        fundo_preto = Image.new("RGBA", img_video.size, (0, 0, 0, 255))
        fundo_preto.paste(img_video, (0, 0), img_video)
        fundo_preto.convert("RGB").save("teste_layout_video.jpg", quality=95)
        log.info("Gerado: teste_layout_video.jpg")

    print("\nAbra os arquivos 'teste_layout_imagem.jpg' e 'teste_layout_video.jpg' para verificar os alinhamentos!")

if __name__ == "__main__":
    MODO_TESTE = False 
    
    if MODO_TESTE:
        executar_teste()
    else:
        modo_escolhido = sys.argv[1] if len(sys.argv) > 1 else "video"
        publicar(modo_escolhido)