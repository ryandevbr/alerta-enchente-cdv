# gerar_manchas.py
# Gera as manchas de inundacao (780-1000 cm) a partir do DEM recortado.
# Saida: manchas_inundacao.geojson (1 FeatureCollection, 5 features).
#
# Requisitos: rasterio, shapely, numpy, pyproj
#   pip install rasterio shapely numpy pyproj

import json
from pathlib import Path

import numpy as np
import rasterio
from rasterio.features import shapes
from shapely.geometry import shape, mapping, Polygon, MultiPolygon
from shapely.ops import unary_union
from pyproj import Transformer

# ============================================================
# PARAMETROS
# ============================================================
DEM_PATH = Path("dados_dem/dem_bairro.tif")   # DEM recortado, em EPSG:31983
OUT_PATH = Path("manchas_inundacao.geojson")
ZERO_REGUA_M = 226.34                          # <-- muda aqui quando a ANA responder
COTAS_CM = [620, 700, 750, 800, 850, 900, 950, 1000, 1050, 1100]  # cotas da regua (cm) para gerar manchas
SIMPLIFY_TOL_M = 15.0       # era 5.0 — 3x mais agressivo
SMOOTH_ITER = 1             # era 3 — reduz o efeito de duplicacao de pontos
SMOOTH_OFFSET = 0.25
MIN_AREA_M2 = 5000          # era 2000 — descarta fragmentos menores
# ============================================================


def cota_para_elevacao(cota_cm: float) -> float:
    """Converte cota da regua (cm) em elevacao absoluta (m)."""
    return ZERO_REGUA_M + cota_cm / 100.0


def chaikin(ring_coords, iterations=SMOOTH_ITER, offset=SMOOTH_OFFSET):
    """Suavizacao de Chaikin (corner-cutting) para um anel fechado."""
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
    """Aplica Chaikin no exterior e nos interiores, removendo aneis degenerados."""
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

def arredondar_coords(geom, precision=5):
    """Arredonda coordenadas para reduzir tamanho do GeoJSON."""
    from shapely.ops import transform
    return transform(
        lambda x, y, z=None: (round(x, precision), round(y, precision)),
        geom
    )

def gerar_mancha(dem_path: Path, elev_alvo: float):
    """Retorna geometria (MultiPolygon) da area com DEM <= elev_alvo."""
    with rasterio.open(dem_path) as src:
        dem = src.read(1)
        transform = src.transform
        crs = src.crs
        nodata = src.nodata

        if crs is None or crs.to_epsg() != 31983:
            raise ValueError(
                f"DEM precisa estar em EPSG:31983 (UTM 23S). Encontrado: {crs}"
            )

        valido = (dem != nodata) if nodata is not None else np.ones_like(dem, dtype=bool)
        mascara = ((dem <= elev_alvo) & valido).astype(np.uint8)

        polys = [
            shape(geom)
            for geom, val in shapes(mascara, mask=mascara.astype(bool), transform=transform)
            if val == 1
        ]
        if not polys:
            return None

        uniao = unary_union(polys)

        # Remove poligonos muito pequenos (ruido)
        if isinstance(uniao, MultiPolygon):
            partes = [p for p in uniao.geoms if p.area >= MIN_AREA_M2]
            if not partes:
                return None
            uniao = MultiPolygon(partes) if len(partes) > 1 else partes[0]

        # Simplifica em metros (CRS projetado)
        uniao = uniao.simplify(SIMPLIFY_TOL_M, preserve_topology=True)

        # Suaviza (Chaikin)
        if isinstance(uniao, Polygon):
            uniao = smooth_polygon(uniao)
        elif isinstance(uniao, MultiPolygon):
            suaves = [smooth_polygon(p) for p in uniao.geoms]
            uniao = MultiPolygon(suaves) if len(suaves) > 1 else suaves[0]

        return uniao


def main():
    transformer = Transformer.from_crs("EPSG:31983", "EPSG:4326", always_xy=True)
    features = []

    for cota_cm in COTAS_CM:
        elev = cota_para_elevacao(cota_cm)
        print(f"[{cota_cm} cm] elevacao alvo = {elev:.2f} m ...")
        geom = gerar_mancha(DEM_PATH, elev)
        if geom is None:
            print("  -> vazio (nenhuma area abaixo da cota)")
            continue

        # Reprojeta para WGS84
        geom_wgs = shape(
            __import__("shapely.ops", fromlist=["transform"]).transform(
                transformer.transform, geom
            )
        )
        n_polys = len(geom_wgs.geoms) if hasattr(geom_wgs, "geoms") else 1
        print(f"  -> {n_polys} poligono(s), area = {geom.area/1e6:.3f} km2")

        geom_wgs = arredondar_coords(geom_wgs, precision=5)

        features.append({
            "type": "Feature",
            "properties": {"cota": cota_cm},
            "geometry": mapping(geom_wgs),
        })

    fc = {"type": "FeatureCollection", "features": features}
    OUT_PATH.write_text(json.dumps(fc, ensure_ascii=False), encoding="utf-8")
    print(f"\nOK: {OUT_PATH} ({OUT_PATH.stat().st_size/1024:.1f} KB)")


if __name__ == "__main__":
    main()