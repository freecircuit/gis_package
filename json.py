from concurrent.futures import ThreadPoolExecutor, as_completed
import requests
import geopandas as gpd
import pandas as pd
from shapely.geometry import shape, Point, Polygon, MultiPolygon, LineString
from urllib.parse import urlparse

def download_feature_layer(url, chunk_size=1000, bbox=None, where="1=1", source_name=None):
    """
    Download features from an ArcGIS FeatureServer layer with pagination.
    This remains your core single-threaded engine called by the grid worker.
    """
    features = []
    offset = 0
    spatial_reference = None

    def _bbox_2_envelope(bbox_input):
        if bbox_input is None:
            return None
        if hasattr(bbox_input, 'total_bounds'):
            minx, miny, maxx, maxy = bbox_input.total_bounds
        elif hasattr(bbox_input, "bounds"):
            minx, miny, maxx, maxy = bbox_input.bounds
        elif isinstance(bbox_input, (list, tuple)) and len(bbox_input) == 4:
            minx, miny, maxx, maxy = bbox_input
        else:
            raise ValueError("bbox must be tuple, shapely geometry, or GeoDataFrame/GeoSeries.")
        return f"{minx},{miny},{maxx},{maxy}"
        
    envelope = _bbox_2_envelope(bbox)

    while True:
        params = {
            'where': where,
            'outFields': '*',
            'f': 'geojson',
            'resultOffset': offset,
            'resultRecordCount': chunk_size
        }
        
        if envelope:
            params.update({
                "geometry": envelope,
                "geometryType": "esriGeometryEnvelope",
                "spatialRel": "esriSpatialRelIntersects",
            })
            
        try:
            r = requests.get(url, params=params, timeout=30)
            r.raise_for_status()
            data = r.json()
        except Exception as e:
            print(f"Error fetching data at offset {offset}: {e}")
            break
        
        if not spatial_reference:
            if "crs" in data and "properties" in data["crs"]:
                spatial_reference = data["crs"]["properties"].get("name", "EPSG:4326")
            elif "spatialReference" in data:
                spatial_reference = data["spatialReference"].get("wkid", 4326)

        feats = data.get('features', [])
        if not feats:
            break
        features.extend(feats)
        
        if len(feats) < chunk_size:
            break
            
        offset += chunk_size

    if not features:
        return gpd.GeoDataFrame(columns=['geometry'])

    records = []
    for f in features:
        geom = None
        if "geometry" in f and f["geometry"]:
            g = f["geometry"]
            try:
                geom = shape(g)
            except Exception:
                if "x" in g and "y" in g:
                    geom = Point(g["x"], g["y"])
                elif "rings" in g:
                    try:
                        geom = Polygon(shell=g["rings"][0], holes=g["rings"][1:])
                    except Exception:
                        geom = MultiPolygon([Polygon(r) for r in g["rings"]])
                elif "paths" in g:
                    geom = LineString(g["paths"])

        props = f.get("properties", f.get("attributes", {})) or {}
        rec = {"geometry": geom, **props}
        records.append(rec)

    crs_input = spatial_reference if spatial_reference else "EPSG:4326"
    gdf = gpd.GeoDataFrame(records, geometry="geometry", crs=crs_input)
    
    if not source_name:
        parsed = urlparse(url)
        path_parts = [p for p in parsed.path.split('/') if p]
        source_name = path_parts[-2] if len(path_parts) >= 2 else parsed.netloc
    gdf['source'] = source_name

    return gdf


def download_feature_layer_parallel(url, bbox, grid_size=4, max_workers=4, chunk_size=1000, where="1=1", source_name=None):
    """
    Splits a target bounding box into a grid and downloads features using parallel threads.
    
    grid_size: Split the bbox into a X by X grid (e.g., 4 creates 16 spatial squares).
    max_workers: Maximum number of concurrent network threads to spin up.
    """
    # 1. Parse bounding box coordinates uniformly
    if hasattr(bbox, 'total_bounds'):
        xmin, ymin, xmax, ymax = bbox.total_bounds
    elif hasattr(bbox, "bounds"):
        xmin, ymin, xmax, ymax = bbox.bounds
    elif isinstance(bbox, (list, tuple)) and len(bbox) == 4:
        xmin, ymin, xmax, ymax = bbox
    else:
        raise ValueError("bbox must be a tuple, list, shapely geometry, or GeoDataFrame/GeoSeries.")

    # 2. Compute grid dimensions
    x_step = (xmax - xmin) / grid_size
    y_step = (ymax - ymin) / grid_size
    
    grid_boxes = []
    for i in range(grid_size):
        for j in range(grid_size):
            b_xmin = xmin + (i * x_step)
            b_ymin = ymin + (j * y_step)
            b_xmax = b_xmin + x_step
            b_ymax = b_ymin + y_step
            grid_boxes.append([b_xmin, b_ymin, b_xmax, b_ymax])

    print(f"Divided target area into {len(grid_boxes)} spatial chunks. Starting download using {max_workers} parallel workers...")

    all_gdfs = []
    
    # 3. Execute queries using an asynchronous network pool thread executor
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        # Submit all tasks to the queue background workers
        future_to_bbox = {
            executor.submit(download_feature_layer, url, chunk_size, box, where, source_name): box 
            for box in grid_boxes
        }
        
        # Collect results as workers finish processing each box
        completed_count = 0
        for future in as_completed(future_to_bbox):
            completed_count += 1
            box = future_to_bbox[future]
            try:
                gdf_chunk = future.result()
                if not gdf_chunk.empty and 'geometry' in gdf_chunk.columns:
                    all_gdfs.append(gdf_chunk)
                    print(f"[{completed_count}/{len(grid_boxes)}] Worker finished. Extracted {len(gdf_chunk)} features.")
                else:
                    print(f"[{completed_count}/{len(grid_boxes)}] Worker finished. (0 features in this grid zone).")
            except Exception as exc:
                print(f"Grid box {box} generated an execution error: {exc}")

    if not all_gdfs:
        print("Parallel compilation complete. Zero features found.")
        return gpd.GeoDataFrame(columns=['geometry'])

    # 4. Standardize and drop spatial duplication caused by overlapping feature boundaries 
    print("Combining spatial blocks and dropping duplicate server elements...")
    combined_gdf = gpd.GeoDataFrame(pd.concat(all_gdfs, ignore_index=True), geometry='geometry')
    
    # Drop records that span multiple boxes by validating unique ObjectIDs
    id_column = next((col for col in combined_gdf.columns if col.upper() in ['OBJECTID', 'FID', 'ID']), None)
    if id_column:
        combined_gdf = combined_gdf.drop_duplicates(subset=[id_column])
    
    print(f"Parallel Pipeline Complete. Downloaded unique features count: {len(combined_gdf)}")
    return combined_gdf


def combine_layers_parallel(urls, bbox, grid_size=4, max_workers=4, chunk_size=1000, where='1=1', source_names=None):
    """
    Download multiple massive FeatureServer layers in parallel and combine them into a single GeoDataFrame.
    """
    all_gdfs = []
    if source_names and len(source_names) != len(urls):
        raise ValueError('If provided, source_names must match number of URLs.')

    for i, url in enumerate(urls):
        name = source_names[i] if source_names else None
        
        # 1. Format the URL to target the endpoint query processor
        query_url = url if url.endswith('/query') else f"{url.rstrip('/')}/query"
        
        # 2. Call your new multi-threaded parallel downloader instead of the single-threaded one
        gdf = download_feature_layer_parallel(
            url=query_url, 
            bbox=bbox, 
            grid_size=grid_size, 
            max_workers=max_workers, 
            chunk_size=chunk_size, 
            where=where, 
            source_name=name
        )
        
        if not gdf.empty:
            all_gdfs.append(gdf)

    if all_gdfs:
        # 3. Stack all the layers together. 
        # Pandas concat handles mismatched table columns across different layers natively by padding with NaNs.
        combined_gdf = gpd.GeoDataFrame(pd.concat(all_gdfs, ignore_index=True), geometry='geometry')
        print(f'Combined total features: {len(combined_gdf)} from {len(urls)} distinct layers')
        return combined_gdf
    else:
        print("No valid features to combine")
        return gpd.GeoDataFrame(columns=['geometry'])
