"""Utilitário para comprimir coordenadas de arquivos GeoJSON em linha única."""

import argparse
import json
from pathlib import Path
import sys

RAIZ = Path(__file__).resolve().parent.parent.parent
ARQUIVO_PADRAO = RAIZ / "docs" / "manchas_arquivadas" / "exemplo_bruto.geojson"
COTA_PADRAO = 400


def formatar_geojson(texto_json: str, cota: int) -> str:
    """
    Lê uma string GeoJSON (FeatureCollection ou Feature) e devolve os blocos
    de features formatados com as coordenadas comprimidas em uma única linha.
    """
    dados = json.loads(texto_json.strip())

    if dados.get("type") == "FeatureCollection":
        features = dados.get("features", [])
    else:
        features = [dados]

    resultados: list[str] = []

    for feat in features:
        geometria = feat.get("geometry")
        if not geometria:
            continue

        tipo_geometria = geometria.get("type", "Polygon")
        coords_compactas = json.dumps(
            geometria.get("coordinates", []),
            separators=(", ", ": "),
        )

        bloco_formatado = (
            "{\n"
            '    "type": "Feature",\n'
            f'    "properties": {{ "cota": {cota} }},\n'
            '    "geometry": {\n'
            f'        "type": "{tipo_geometria}",\n'
            f'        "coordinates": {coords_compactas}\n'
            "    }\n"
            "}"
        )
        resultados.append(bloco_formatado)

    return ",\n".join(resultados)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Comprime as coordenadas de um arquivo GeoJSON para uso no mapa."
    )
    parser.add_argument(
        "arquivo",
        nargs="?",
        default=str(ARQUIVO_PADRAO),
        help="Caminho para o arquivo .geojson bruto (padrão: exemplo_bruto.geojson)",
    )
    parser.add_argument(
        "--cota",
        type=int,
        default=COTA_PADRAO,
        help=f"Cota da régua em cm associada à feição (padrão: {COTA_PADRAO})",
    )
    args = parser.parse_args()

    caminho = Path(args.arquivo)
    if not caminho.exists():
        print(f"Erro: arquivo não encontrado: {caminho}", file=sys.stderr)
        return 1

    try:
        texto_bruto = caminho.read_text(encoding="utf-8")
        texto_final = formatar_geojson(texto_bruto, args.cota)
    except json.JSONDecodeError:
        print(
            "Erro: o conteúdo informado não é um JSON válido.",
            file=sys.stderr,
        )
        return 1

    print("\n" + "=" * 50)
    print(" SCRIPT LIMPO! SÓ COPIAR E COLAR:")
    print("=" * 50 + "\n")
    print(texto_final)
    print("\n" + "=" * 50)
    return 0


if __name__ == "__main__":
    sys.exit(main())
