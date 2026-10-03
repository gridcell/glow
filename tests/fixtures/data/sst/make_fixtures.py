"""Write two small synthetic SST netCDF files. Runs inside the gdal image.

    python3 make_fixtures.py <directory>

Generating them keeps binary files out of the repository. Each file is a
20x10 float32 gradient over 130W to 120W and 45N to 50N, in the variable
`sea_surface_temperature`; the second day is one degree warmer.
"""

import struct
import sys
from pathlib import Path

from osgeo import gdal, osr

WIDTH, HEIGHT = 20, 10
VARIABLE = "sea_surface_temperature"
DAYS = {"20240101": 0.0, "20240102": 1.0}


def main() -> None:
    gdal.UseExceptions()
    directory = Path(sys.argv[1])
    directory.mkdir(parents=True, exist_ok=True)
    srs = osr.SpatialReference()
    srs.ImportFromEPSG(4326)
    for day, offset in DAYS.items():
        raster = gdal.GetDriverByName("MEM").Create("", WIDTH, HEIGHT, 1, gdal.GDT_Float32)
        raster.SetGeoTransform([-130.0, 0.5, 0.0, 50.0, 0.0, -0.5])
        raster.SetProjection(srs.ExportToWkt())
        band = raster.GetRasterBand(1)
        values = [offset + column * 1.5 for _ in range(HEIGHT) for column in range(WIDTH)]
        band.WriteRaster(0, 0, WIDTH, HEIGHT, struct.pack(f"{WIDTH * HEIGHT}f", *values))
        band.SetNoDataValue(-999.0)
        # The netCDF driver names the variable after this band metadata item.
        band.SetMetadataItem("NETCDF_VARNAME", VARIABLE)
        gdal.Translate(str(directory / f"sst_{day}.nc"), raster, format="netCDF")


if __name__ == "__main__":
    main()
