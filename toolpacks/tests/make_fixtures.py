"""Write the raster fixtures for the wrapper tests. Runs inside the gdal image.

Generating them keeps binary files out of the repository. Both rasters are a
20x10 float32 gradient from 0 to 28.5 over 130W to 120W and 45N to 50N.
"""

import struct
import sys
from pathlib import Path

from osgeo import gdal, osr

WIDTH, HEIGHT = 20, 10
VARIABLE = "sea_surface_temperature"


def main() -> None:
    gdal.UseExceptions()
    directory = Path(sys.argv[1])
    directory.mkdir(parents=True, exist_ok=True)
    srs = osr.SpatialReference()
    srs.ImportFromEPSG(4326)
    raster = gdal.GetDriverByName("MEM").Create("", WIDTH, HEIGHT, 1, gdal.GDT_Float32)
    raster.SetGeoTransform([-130.0, 0.5, 0.0, 50.0, 0.0, -0.5])
    raster.SetProjection(srs.ExportToWkt())
    band = raster.GetRasterBand(1)
    values = [column * 1.5 for _ in range(HEIGHT) for column in range(WIDTH)]
    band.WriteRaster(0, 0, WIDTH, HEIGHT, struct.pack(f"{WIDTH * HEIGHT}f", *values))
    band.SetNoDataValue(-999.0)
    # The netCDF driver names the variable after this band metadata item.
    band.SetMetadataItem("NETCDF_VARNAME", VARIABLE)
    gdal.Translate(str(directory / "sst_20240101.nc"), raster, format="netCDF")
    gdal.Translate(str(directory / "raster.tif"), raster, format="GTiff")


if __name__ == "__main__":
    main()
