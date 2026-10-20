# gerar_manchas.py
# Gera as manchas de inundação (620-1100 cm) a partir do DEM recortado.
# Saída: manchas_inundacao.geojson (1 FeatureCollection).
#
# Requisitos: rasterio, shapely, numpy, pyproj
#   pip install rasterio shapely numpy pyproj

import json
from pathlib import Path
from typing import Any, Optional

import numpy as np
from pyproj import Transformer
import rasterio
from rasterio.features import shapes
from shapely.geometry import MultiPolygon, Polygon, mapping, shape
from shapely.ops import transform as shapely_transform, unary_union

# ============================================================
# PARÂMETROS
# ============================================================
DEM_PATH = Path("dados_dem/dem_bairro.tif")  # DEM recortado, em EPSG:31983
OUT_PATH = Path("manchas_inundacao.geojson")
ZERO_REGUA_M = 226.34  # <-- muda aqui quando a ANA responder
COTAS_CM = [620, 700, 750, 800, 850, 900, 950, 1000, 1050, 1100]
SIMPLIFY_TOL_M = 15.0  # era 5.0 — 3x mais agressivo
SMOOTH_ITER = 1        # era 3 — reduz o efeito de duplicação de pontos
SMOOTH_OFFSET = 0.25
MIN_AREA_M2 = 5000     # era 2000 — descarta fragmentos menores
# ============================================================


def cota_para_elevacao(cota_cm: float) -> float:
    """Converte cota da régua (cm) em elevação absoluta (m)."""
    return ZERO_REGUA_M + cota_cm / 100.0


def chaikin(
    ring_coords: Any,
    iterations: int = SMOOTH_ITER,
    offset: float = SMOOTH_OFFSET,
) -> list[tuple[float, float]]:
    """Suavização de Chaikin (corner-cutting) para um anel fechado."""
    coords = list(ring_coords)
    for _ in range(iterations):
        new = [coords[0]]
        for i in range(len(coords) - 1):
            x0, y0 = coords[i]
            x1, y1 = coords[i + 1]
            qx = x0 + offset * (x1 - x0)
            qy = y0 + offset * (y1 - y0)
            rx = x0 + (1 - offset) * (x1 - x0)
            ry = y0 + (1 - offset) * (y1 - y0)
            new.append((qx, qy))
            new.append((rx, ry))
        new.append(coords[-1])
        coords = new
    return coords


def smooth_polygon(geom: Polygon) -> Polygon:
    """Aplica Chaikin no exterior e nos interiores, removendo anéis degenerados."""
    ext = chaikin(geom.exterior.coords)
    new_ext = Polygon(ext).exterior
    interiors = []
    for hole in geom.interiors:
        h = chaikin(hole.coords)
        if len(h) >= 4:
            interiors.append(h)
    result = Polygon(new_ext, interiors)
    if not result.is_valid:
        result = result.buffer(0)
    return result


def arredondar_coords(geom: Any, precision: int = 5) -> Any:
    """Arredonda coordenadas para reduzir tamanho do GeoJSON."""
    return shapely_transform(
        lambda x, y, z=None: (round(x, precision), round(y, precision)),
        geom,
    )


def carregar_dem(dem_path: Path) -> tuple[np.ndarray, np.ndarray, Any]:
    """
    Lê o arquivo GeoTIFF do DEM uma única vez em memória.
    Retorna (dem_array, mascara_valido, affine_transform).
    """
    with rasterio.open(dem_path) as src:
        crs = src.crs
        if crs is None or crs.to_epsg() != 31983:
            raise ValueError(
                f"DEM precisa estar em EPSG:31983 (UTM 23S). Encontrado: {crs}"
            )
        dem = src.read(1)
        affine_transform = src.transform
        nodata = src.nodata

    valido = (
        (dem != nodata) if nodata is not None else np.ones_like(dem, dtype=bool)
    )
    return dem, valido, affine_transform


def gerar_mancha_de_matriz(
    dem: np.ndarray,
    valido: np.ndarray,
    affine_transform: Any,
    elev_alvo: float,
) -> Optional[Polygon | MultiPolygon]:
    """Retorna geometria (Polygon | MultiPolygon) da área com DEM <= elev_alvo."""
    mascara = ((dem <= elev_alvo) & valido).astype(np.uint8)

    polys = [
        shape(geom)
        for geom, val in shapes(
            mascara, mask=mascara.astype(bool), transform=affine_transform
        )
        if val == 1
    ]
    if not polys:
        return None

    uniao = unary_union(polys)

    # Remove polígonos muito pequenos (ruído)
    if isinstance(uniao, MultiPolygon):
        partes = [p for p in uniao.geoms if p.area >= MIN_AREA_M2]
        if not partes:
            return None
        uniao = MultiPolygon(partes) if len(partes) > 1 else partes[0]
    elif isinstance(uniao, Polygon) and uniao.area < MIN_AREA_M2:
        return None

    # Simplifica em metros (CRS projetado)
    uniao = uniao.simplify(SIMPLIFY_TOL_M, preserve_topology=True)

    # Suaviza (Chaikin)
    if isinstance(uniao, Polygon):
        uniao = smooth_polygon(uniao)
    elif isinstance(uniao, MultiPolygon):
        suaves = [smooth_polygon(p) for p in uniao.geoms]
        uniao = MultiPolygon(suaves) if len(suaves) > 1 else suaves[0]

    return uniao


def gerar_mancha(
    dem_path: Path, elev_alvo: float
) -> Optional[Polygon | MultiPolygon]:
    """Wrapper de compatibilidade para gerar uma mancha única a partir de `dem_path`."""
    dem, valido, affine_transform = carregar_dem(dem_path)
    return gerar_mancha_de_matriz(dem, valido, affine_transform, elev_alvo)


def main() -> None:
    transformer = Transformer.from_crs(
        "EPSG:31983", "EPSG:4326", always_xy=True
    )
    dem, valido, affine_transform = carregar_dem(DEM_PATH)
    features = []

    for cota_cm in COTAS_CM:
        elev = cota_para_elevacao(cota_cm)
        print(f"[{cota_cm} cm] elevação alvo = {elev:.2f} m ...")
        geom = gerar_mancha_de_matriz(dem, valido, affine_transform, elev)
        if geom is None:
            print("  -> vazio (nenhuma área abaixo da cota)")
            continue

        # Reprojeta para WGS84
        geom_wgs = shapely_transform(transformer.transform, geom)
        n_polys = len(geom_wgs.geoms) if hasattr(geom_wgs, "geoms") else 1
        print(f"  -> {n_polys} polígono(s), área = {geom.area / 1e6:.3f} km²")

        geom_wgs = arredondar_coords(geom_wgs, precision=5)

        features.append(
            {
                "type": "Feature",
                "properties": {"cota": cota_cm},
                "geometry": mapping(geom_wgs),
            }
        )

    fc = {"type": "FeatureCollection", "features": features}
    OUT_PATH.write_text(json.dumps(fc, ensure_ascii=False), encoding="utf-8")
    print(f"\nOK: {OUT_PATH} ({OUT_PATH.stat().st_size / 1024:.1f} KB)")


if __name__ == "__main__":
    main()
