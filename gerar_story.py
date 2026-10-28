import os
import io
from datetime import datetime, timezone
from PIL import Image, ImageDraw, ImageFont
from core.config import configurar_logger
from core.db import obter_supabase

log = configurar_logger("story", formato_data="%H:%M:%S")

# --- ARQUIVOS DE FONTE ---
FONTE_BOLD = "assets/OpenSans-Bold.ttf"
FONTE_REGULAR = "assets/OpenSans-Regular.ttf"

# =========================================================
# COORDENADAS (X, Y) - AJUSTE AQUI ATÉ FICAR PERFEITO
# =========================================================
POS_NIVEL = (540, 645)       

POS_HOJE = (430, 1050)       
POS_ANO1 = (750, 1050)       # Quadrante superior direito
POS_ANO2 = (430, 1150)       # Quadrante inferior esquerdo
POS_ANO3 = (750, 1150)       # Quadrante inferior direito

POS_DOT_CEMIG = (390, 1403)  
RAIO_DOT = 28                
POS_DEFLUENCIA = (430, 1405) 

# =========================================================
# CONFIGURAÇÃO FINAL
# =========================================================
MODO_TESTE = False  # Mude para True se quiser gerar as imagens de teste

def obter_dados():
    supabase = obter_supabase()
    try:
        r_status = supabase.table("cache_site").select("dados").eq("chave", "status_atual").execute()
        dados_status = r_status.data[0]["dados"]
        nivel = float(dados_status["nivel_atual"])
        defluencia = float(dados_status.get("defluencia_cemig") or 0.0)
        
        r_comp = supabase.table("cache_site").select("dados").eq("chave", "comparativo_anual").execute()
        comp = r_comp.data[0]["dados"]
        anos = {a["ano"]: a["nivel_cm"] for a in comp.get("anos_anteriores", [])}
        
        return nivel, defluencia, anos, supabase
    except Exception as e:
        log.error(f"Erro ao buscar dados no Supabase: {e}")
        return None, None, None, supabase

def obter_cor_defluencia(defluencia):
    if defluencia >= 550: return "#ef4444" # Vermelho
    if defluencia >= 100: return "#fb923c" # Laranja
    if defluencia >= 30:  return "#facc15" # Amarelo
    return "#3b82f6"                       # Azul

def obter_anos_comparacao():
    """Define os anos de comparação com base no ano atual."""
    ano_atual = datetime.now().year
    if ano_atual == 2026:
        return [2023, 2021, 2018]
    else:
        # Para 2027 em diante
        return [2024, 2022, 2019]

def gerar_story_producao():
    nivel, defluencia, anos, supabase = obter_dados()
    if nivel is None:
        log.error("Não foi possível obter dados. Abortando geração do story.")
        return

    log.info(f"Gerando story REAL: Nível {nivel}cm | Defluência {defluencia}m³/s")

    # 1. Escolher template
    if nivel < 450: template_path = "template_normal.png"
    elif nivel < 540: template_path = "template_atencao.png"
    elif nivel < 620: template_path = "template_alerta.png"
    else: template_path = "template_inundacao.png"

    if not os.path.exists(template_path):
        log.error(f"Arquivo {template_path} não encontrado!")
        return

    img = Image.open(template_path).convert("RGBA")
    draw = ImageDraw.Draw(img)
    
    try:
        font_nivel = ImageFont.truetype(FONTE_BOLD, 140)
        font_anos = ImageFont.truetype(FONTE_BOLD, 40)
        font_cemig = ImageFont.truetype(FONTE_BOLD, 40)
    except IOError:
        log.error("Fontes não encontradas.")
        return

    # 2. Escrever Nível e Comparativo
    draw.text(POS_NIVEL, f"{nivel:.0f} cm", font=font_nivel, fill="white", anchor="mm")
    draw.text(POS_HOJE, f"HOJE: {nivel:.0f}cm", font=font_anos, fill="white", anchor="mm")
    
    # Pega os anos corretos baseados na data atual (Ex: 2023, 2021, 2018)
    anos_alvo = obter_anos_comparacao()
    
    # Escreve o primeiro ano (Superior Direito)
    ano1 = anos_alvo[0]
    valor1 = anos.get(ano1, 0) # Se não achar o ano, coloca 0
    draw.text(POS_ANO1, f"{ano1}: {valor1:.0f}cm", font=font_anos, fill="white", anchor="mm")
    
    # Escreve o segundo ano (Inferior Esquerdo)
    ano2 = anos_alvo[1]
    valor2 = anos.get(ano2, 0)
    draw.text(POS_ANO2, f"{ano2}: {valor2:.0f}cm", font=font_anos, fill="white", anchor="mm")
    
    # Escreve o terceiro ano (Inferior Direito)
    ano3 = anos_alvo[2]
    valor3 = anos.get(ano3, 0)
    draw.text(POS_ANO3, f"{ano3}: {valor3:.0f}cm", font=font_anos, fill="white", anchor="mm")

    # 3. Escrever Defluência
    cor_def = obter_cor_defluencia(defluencia)
    draw.ellipse([
        POS_DOT_CEMIG[0] - RAIO_DOT, POS_DOT_CEMIG[1] - RAIO_DOT, 
        POS_DOT_CEMIG[0] + RAIO_DOT, POS_DOT_CEMIG[1] + RAIO_DOT
    ], fill=cor_def)
    
    texto_def = f"Defluência: {defluencia:.0f} m³/s" if defluencia > 0 else "Defluência: --"
    draw.text(POS_DEFLUENCIA, texto_def, font=font_cemig, fill="white", anchor="lm")

    # 4. Upload para o Supabase Storage
    try:
        img_byte_arr = io.BytesIO()
        img.convert("RGB").save(img_byte_arr, format='JPEG', quality=95)
        img_byte_arr.seek(0)

        nome_arquivo = "ultimo_story.jpg"
        
        # Faz o upload (upsert para sobrescrever a imagem anterior)
        supabase.storage.from_("stories").upload(
            file=img_byte_arr.read(),
            path=nome_arquivo,
            file_options={"content-type": "image/jpeg", "upsert": "true"}
        )
        
        # Pega a URL pública com cache buster
        url_publica_base = supabase.storage.from_("stories").get_public_url(nome_arquivo)
        ts = int(datetime.now().timestamp())
        url_final = f"{url_publica_base}?t={ts}"

        # Salva no cache_site para o MacroDroid ler
        supabase.table("cache_site").upsert({
            "chave": "story_atual",
            "dados": {"url": url_final, "gerado_em": datetime.now(timezone.utc).isoformat()},
            "atualizado_em": datetime.now(timezone.utc).isoformat()
        }, on_conflict="chave").execute()

        log.info(f"✅ Story gerado e enviado! URL: {url_final}")
        
    except Exception as e:
        log.error(f"Erro no upload para o Supabase: {e}")

        # Avisar o Telegram que a imagem está pronta
        import requests
        TOKEN = os.getenv("TELEGRAM_TOKEN")
        CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
        
        if TOKEN and CHAT_ID:
            requests.post(
                f"https://api.telegram.org/bot{TOKEN}/sendMessage",
                json={"chat_id": CHAT_ID, "text": "Story gerado!"}
            )

if __name__ == "__main__":
    if MODO_TESTE:
        log.info("Modo de teste ativado.")
        # Você pode rodar a função de testes aqui se quiser.
    else:
        gerar_story_producao()