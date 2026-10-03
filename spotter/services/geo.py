import numpy as np

EARTH_RADIUS_MI = 3958.7613
METERS_PER_MILE = 1609.344

def to_xyz(lat, lon):
    # Convert lat/lon degrees to 3D Cartesian points on a sphere in miles
    la, lo = np.radians(lat), np.radians(lon)
    xyz_cartesian = EARTH_RADIUS_MI * np.column_stack((np.cos(la) * np.cos(lo), np.cos(la) * np.sin(lo), np.sin(la)))

    return xyz_cartesian

def haversine_mi(lat1, lon1, lat2, lon2):
    # circular distance between points in miles
    p1, p2 = np.radians(lat1), np.radians(lat2)
    a = (
        np.sin((p2 - p1) / 2) ** 2 + np.cos(p1) * np.cos(p2) * np.sin(np.radians(lon2 - lon1) / 2) ** 2
    )

    return 2 * EARTH_RADIUS_MI * np.arcsin(np.sqrt(a))