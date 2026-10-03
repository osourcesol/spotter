import numpy as np

EARTH_RADIUS_MI = 3958.7613

def to_xyz(lat, lon):
    # Convert lat/lon degrees to 3D Cartesian points on a sphere in miles
    la, lo = np.radians(lat), np.radians(lon)
    xyz_cartesian = EARTH_RADIUS_MI * np.column_stack((np.cos(la) * np.cos(lo), np.cos(la) * np.sin(lo), np.sin(la)))

    return xyz_cartesian