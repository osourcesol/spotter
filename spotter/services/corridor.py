from dataclasses import dataclass
import numpy as np
from scipy.spatial import cKDTree

from .geo import to_xyz

@dataclass
class Corridor:
    # stations near the route
    station_index: np.ndarray # position of each station in StationStore
    mile_marker: np.ndarray # miles from the start along the road
    off_route_miles: np.ndarray # straight line distance from the route

    def __len__(self) -> int:
        return len(self.station_index)

def stations_along_route(route, store, radius_miles: float = 5.0) -> Corridor:
    # stations within raidus miles of the route
    tree = cKDTree(to_xyz(route.grid_lat, route.grid_lon))

    """
    for every station, distance to nearest route point
    a station with no route point within radius get distance = inf
    """
    distance, nearest = tree.query(store.xyz, distance_upper_bound=radius_miles)
    near = np.isfinite(distance)

    station_index = np.flatnonzero(near)
    mile_marker = route.grid_miles[nearest[near]]
    off_route = distance[near]

    order = np.argsort(mile_marker, kind="stable")
    return Corridor(station_index[order], mile_marker[order], off_route[order])



