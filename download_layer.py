import requests
import geopandas as gpd
import pandas as pd
from shapely.geometry import shape

def download_feature_layer(url, chunk_size=1000, where="1=1", source_name=None):
    """
    Download all features from an ArcGIS FeatureServer layer with pagination.

    Returns a GeoDataFrame.
    """
    features = []
    offset = 0

    while True:
        params = {
            'where': where,
            'outFields': '*',
            'f': 'geojson',
            'resultOffset': offset,
            'resultRecordCount': chunk_size
        }
        r = requests.get(url, params=params)
        r.raise_for_status()
        data = r.json()
        feats = data.get('features', [])
        if not feats:
            break
        features.extend(feats)
        offset += chunk_size
        print(f"Downloaded {len(features)} features from {url}...")

    print(f"Finished Downloading {len(features)} features from {url}")

    # Convert to GeoDataFrame
    records = []
    for f in features:
        geom = None
        if "geometry" in f and f['geometry']:
            try:
                geom = shape(f['geometry'])
            except Exception:
                geom=None
        props = f.get("properties", {}) or {}
        rec = {'geometry': geom, **props}
        records.append(rec)
    if any(r['geometry'] is not None for r in records):
        gdf = gpd.GeoDataFrame(records, geometry='geometry')
    else:
        gdf = pd.DataFrame(records)

    # Tag Source
    if not source_name:
        # Use the last part of the path as a fallback (e.g. service/layer)
        parsed = urlparse(url)
        source_name = parsed.path.split('/')[-2] if '/' in parsed.path else parsed.netloc
    gdf['source'] = source_name

    return gdf


def combine_layer(urls, chunk_size=1000, where='1=1', source_names=None):
    """
    Download multiple FeatureServer layers and combine into a single GeoDataFrame.
    Automatically normalize columns and tage each record with its source name or URL.
    """
    all_gdfs = []
    if source_names and len(source_names) != len(urls):
        raise ValueError('If provided, source_names must match number of URLs.')

    for i, url in enumerate(urls):
        name = source_names[i] if source_names else None
        gdf = download_feature_layer(url, chunk_size=chunk_size, where=where, source_name=name)
        gdf = normalize_columns(gdf)
        if not gdf.empty:
            all_gdfs.append(gdf)

    if all_gdfs:
        combined_gdf = gpd.GeoDataFrame(pd.concat(all_gdfs, ignore_index=True), geometry='geometry')
        print(f'Combined total features: {len(combined_gdf)} from {len(urls)} layers')
        return combined_gdf
    else:
        print("Np valid features to combine")
        return gpd.GeoDataFrame(columns=['geometry'])
