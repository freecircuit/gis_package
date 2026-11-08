from shapely import MultiPolygon, Polygon, Point

def swap_coords(geom):
    if geom.geom_type == 'Polygon':
        return Polygon([(y, x) for x, y in geom.exterior.coords])
    elif geom.geom_type == 'MultiPolygon':
        return MultiPolygon([
            Polygon([(y, x) for x, y in poly.exterior.coords])
            for poly in geom.geoms
        ])
    if geom.geom_type == 'Point':
        x, y = geom.coords[0]
        return Point(y, x)
    else:
        return geom
