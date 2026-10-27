import io
import os
from datetime import datetime, timezone
from PIL import Image, ImageDraw, ImageFont
from core.config import configurar_logger
from core.db import obter_supabase

log = configurar_logger("story", formato_data="%H:%M:%S")

# AJUSTE ESTES CAMINHOS SE NECESSÁRIO
FONTE_BOLD = "OpenSans-Bold.ttf"
FONTE_REGULAR = "OpenSans-Regular.ttf"

# Coordenadas (X, Y) baseadas no seu design de 1080x1920
# Você pode precisar ajustar esses valores finos depois do primeiro teste
POS_NIVEL = (540, 600)
POS_COMP_HOJE = (400, 1050)
POS_COMP_2025 = (750, 1050)
POS_COMP_2024 = (400, 1180)
POS_COMP_2023 = (750, 1180)
POS_DEFLUENCIA = (540, 1450)

def obter_dados():
    supabase = obter_supabase()
    
    # Pega o status atual (Nível)
    r_status = supabase.table("cache_site").select("dados").eq("chave", "status_atual").execute()
    nivel = float(r_status.data[0]["dados"]["nivel_atual"])
    
    # Pega a defluência
    r_cemig = supabase.table("cache_site").select("dados").eq("chave", "status_atual").execute()
    defluencia = r_cemig.data[0]["dados"].get("defluencia_cemig")
    if defluencia is None: defluencia = 0.0
    
    # Pega o comparativo
    r_comp = supabase.table("cache_site").select("dados").eq("chave", "comparativo_anual").execute()
    comp = r_comp.data[0]["dados"]
    anos = {a["ano"]: a["nivel_cm"] for a in comp.get("anos_anteriores", [])}
    
    return nivel, defluencia, anos

def gerar_story():
    nivel, defluencia, anos = obter_dados()
    log.info(f"Gerando story para Nível {nivel}cm e Defluência {defluencia}m3/s")

    # 1. Escolher o template baseado no nível
    if nivel < 450:
        template_path = "template_normal.png"
        cor_texto = "white"
    elif nivel < 540:
        template_path = "template_atencao.png"
        cor_texto = "#facc15" # Amarelo
    elif nivel < 620:
        template_path = "template_alerta.png"
        cor_texto = "#fb923c" # Laranja
    else:
        template_path = "template_inundacao.png"
        cor_texto = "#ef4444" # Vermelho

    # 2. Abrir imagem e preparar desenho
    try:
        img = Image.open(template_path).convert("RGBA")
    except FileNotFoundError:
        log.error(f"Template {template_path} não encontrado!")
        return None

    draw = ImageDraw.Draw(img)
    
    try:
        font_grande = ImageFont.truetype(FONTE_BOLD, 130)
        font_media = ImageFont.truetype(FONTE_BOLD, 50)
        font_pequena = ImageFont.truetype(FONTE_REGULAR, 45)
    except IOError:
        log.warning("Fontes Montserrat não encontradas. Usando fonte padrão.")
        font_grande = font_media = font_pequena = ImageFont.load_default()

    # 3. Escrever Nível Central
    draw.text(POS_NIVEL, f"{nivel:.0f} cm", font=font_grande, fill=cor_texto, anchor="mm")

    # 4. Escrever Comparativo (Pega os 3 anos mais recentes disponíveis)
    anos_recentes = sorted(anos.keys(), reverse=True)[:3]
    posicoes = [POS_COMP_2025, POS_COMP_2024, POS_COMP_2023]
    
    draw.text(POS_COMP_HOJE, f"HOJE: {nivel:.0f}", font=font_media, fill="white", anchor="mm")
    
    for i, ano in enumerate(anos_recentes):
        if i < len(posicoes):
            texto = f"{ano}: {anos[ano]:.0f}"
            draw.text(posicoes[i], texto, font=font_media, fill="white", anchor="mm")

    # 5. Escrever Defluência
    if defluencia > 0:
        texto_def = f"Defluência: {defluencia:.0f} m³/s"
        draw.text(POS_DEFLUENCIA, texto_def, font=font_pequena, fill="white", anchor="mm")
    else:
        draw.text(POS_DEFLUENCIA, "Sem dados", font=font_pequena, fill="gray", anchor="mm")

    # 6. Salvar e fazer upload para o Supabase Storage
    supabase = obter_supabase()
    img_byte_arr = io.BytesIO()
    img.convert("RGB").save(img_byte_arr, format='JPEG', quality=90)
    img_byte_arr.seek(0)

    nome_arquivo = "ultimo_story.jpg"
    try:
        # Faz o upload para o bucket 'stories'
        supabase.storage.from_("stories").upload(
            file=img_byte_arr.read(),
            path=nome_arquivo,
            file_options={"content-type": "image/jpeg", "upsert": "true"}
        )
        log.info("Story enviado para o Supabase Storage.")
        
        # 7. Pega a URL pública e salva no cache_site
        url_publica = supabase.storage.from_("stories").get_public_url(nome_arquivo)
        
        supabase.table("cache_site").upsert({
            "chave": "story_atual",
            "dados": {"url": url_publica, "gerado_em": datetime.now(timezone.utc).isoformat()},
            "atualizado_em": datetime.now(timezone.utc).isoformat()
        }, on_conflict="chave").execute()
        
        log.info(f"Link salvo no banco: {url_publica}")
        return url_publica

    except Exception as e:
        log.error(f"Erro no upload: {e}")
        return None

if __name__ == "__main__":
    gerar_story()