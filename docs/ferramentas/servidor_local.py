"""Servidor HTTP local que compila templates HTML com variáveis do `.env`."""

import http.server
import os
from pathlib import Path
import socketserver
import sys

from dotenv import load_dotenv

# Sobe 3 níveis para achar a raiz do projeto (docs/ferramentas/servidor_local.py)
RAIZ = Path(__file__).resolve().parent.parent.parent
PORT = 8000

ARQUIVOS_TEMPLATE = [
    ("index_base.html", "index.html"),
    ("moderacao_base.html", "moderacao.html"),
]


def gerar_arquivos_html() -> None:
    """Substitui placeholders de configuração nos arquivos HTML base."""
    load_dotenv(RAIZ / ".env")
    mapbox_key = os.getenv("MAPBOX_KEY", "")
    supabase_url = os.getenv("SUPABASE_URL", "")
    supabase_anon_key = os.getenv("SUPABASE_ANON_KEY", "")

    for fonte, saida in ARQUIVOS_TEMPLATE:
        caminho_fonte = RAIZ / fonte
        caminho_saida = RAIZ / saida

        html = caminho_fonte.read_text(encoding="utf-8")
        html = html.replace("CHAVE_MAPBOX_AQUI", mapbox_key)
        html = html.replace("URL_SUPABASE_AQUI", supabase_url)
        html = html.replace("CHAVE_SUPABASE_AQUI", supabase_anon_key)

        caminho_saida.write_text(html, encoding="utf-8")
        print(f"Gerado: {saida}")


def main() -> int:
    os.chdir(RAIZ)
    gerar_arquivos_html()

    handler = http.server.SimpleHTTPRequestHandler
    with socketserver.TCPServer(("", PORT), handler) as httpd:
        print(f"Servidor rodando em http://localhost:{PORT}")
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\nServidor encerrado.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
