import os
import http.server
import socketserver
from pathlib import Path
from dotenv import load_dotenv

# Sobe 3 niveis para achar a raiz do projeto (docs/ferramentas/servidor_local.py)
RAIZ = Path(__file__).resolve().parent.parent.parent
os.chdir(RAIZ)

load_dotenv()
MAPBOX_KEY        = os.getenv("MAPBOX_KEY")
SUPABASE_URL      = os.getenv("SUPABASE_URL")
SUPABASE_ANON_KEY = os.getenv("SUPABASE_ANON_KEY")

arquivos = [
    ("index_base.html",     "index.html"),
    ("moderacao_base.html", "moderacao.html"),
]

for fonte, saida in arquivos:
    with open(fonte, "r", encoding="utf-8") as f:
        html = f.read()

    html = html.replace("CHAVE_MAPBOX_AQUI",   MAPBOX_KEY or "")
    html = html.replace("URL_SUPABASE_AQUI",   SUPABASE_URL or "")
    html = html.replace("CHAVE_SUPABASE_AQUI", SUPABASE_ANON_KEY or "")

    with open(saida, "w", encoding="utf-8") as f:
        f.write(html)

    print(f"Gerado: {saida}")

PORT = 8000
Handler = http.server.SimpleHTTPRequestHandler
with socketserver.TCPServer(("", PORT), Handler) as httpd:
    print(f"Servidor rodando em http://localhost:{PORT}")
    httpd.serve_forever()