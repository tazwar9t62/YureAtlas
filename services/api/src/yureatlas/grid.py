"""EPSG:6933 squares clipped to the exact half-open study envelope."""
import math
from dataclasses import dataclass
from functools import cached_property

import numpy as np
from pyproj import Geod, Transformer
from shapely.geometry import box, mapping
from shapely.ops import transform

from .artifacts import digest

FORWARD = Transformer.from_crs(4326, 6933, always_xy=True)
INVERSE = Transformer.from_crs(6933, 4326, always_xy=True)
GEOD = Geod(ellps="WGS84")


@dataclass(frozen=True)
class StudyGrid:
    west: float = 122
    south: float = 20
    east: float = 156
    north: float = 48
    size_m: int = 50000

    @cached_property
    def limits(self):
        x0, y0 = FORWARD.transform(self.west, self.south)
        x1, y1 = FORWARD.transform(self.east, self.north)
        return x0, y0, x1, y1

    @cached_property
    def indices(self):
        x0, y0, x1, y1 = self.limits
        return [(i, j) for j in range(math.floor(y0 / self.size_m), math.ceil(y1 / self.size_m))
                for i in range(math.floor(x0 / self.size_m), math.ceil(x1 / self.size_m))]

    @cached_property
    def ids(self):
        return [f"{i}:{j}" for i, j in self.indices]

    @cached_property
    def lookup(self):
        return {cell: index for index, cell in enumerate(self.ids)}

    def assign_projected(self, x: float, y: float) -> str:
        # Near-integer roundoff from inverse/forward projection belongs to the
        # north/east cell, within 0.05 mm of a mathematical shared edge.
        qx, qy = x / self.size_m, y / self.size_m
        qx = round(qx) if abs(qx - round(qx)) < 1e-9 else qx
        qy = round(qy) if abs(qy - round(qy)) < 1e-9 else qy
        return f"{math.floor(qx)}:{math.floor(qy)}"

    def assign(self, longitude: float, latitude: float) -> str | None:
        if not self.west <= longitude < self.east or not self.south <= latitude < self.north:
            return None
        return self.assign_projected(*FORWARD.transform(longitude, latitude))

    @cached_property
    def cells(self):
        domain = box(*self.limits)
        result = []
        for i, j in self.indices:
            polygon = box(i*self.size_m, j*self.size_m, (i+1)*self.size_m, (j+1)*self.size_m)
            polygon = transform(INVERSE.transform, polygon.intersection(domain))
            area, _ = GEOD.geometry_area_perimeter(polygon)
            result.append({"id": f"{i}:{j}", "ix": i, "iy": j,
                           "area_km2": abs(area) / 1e6, "geojson": mapping(polygon)})
        return result

    @cached_property
    def centers_lonlat(self):
        return np.array([box(*self.limits).intersection(box(i*self.size_m, j*self.size_m,
                          (i+1)*self.size_m, (j+1)*self.size_m)).centroid.coords[0] for i, j in self.indices])

    @cached_property
    def lonlat(self):
        x, y = self.centers_lonlat.T
        return np.array(INVERSE.transform(x, y)).T

    @cached_property
    def areas(self):
        return np.array([c["area_km2"] for c in self.cells])

    @property
    def spec(self):
        return {"west": self.west, "south": self.south, "east": self.east, "north": self.north,
                "size_m": self.size_m, "crs": "EPSG:6933", "boundary": "west/south included; east/north excluded",
                "cell_count": len(self.ids), "context_buffer_km": 0}

    @property
    def hash(self):
        return digest({"spec": self.spec, "cells": self.cells})

    @classmethod
    def from_spec(cls, spec):
        return cls(**{k: spec[k] for k in ("west", "south", "east", "north", "size_m")})
