import os
import json
import tempfile
import zipfile
import io
import base64
from pathlib import Path

import fiona
import geopandas as gpd
import numpy as np
from flask import Flask, jsonify, render_template, request, send_file, abort

app = Flask(__name__)

# ── Config ────────────────────────────────────────────────────────────────────
UPLOAD_FOLDER = os.path.join(os.path.dirname(__file__), "uploads")
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

_layer_cache: dict = {}
_raster_cache: dict = {}
_active_gdb: str = ""
_raster_files: list = []   # list of absolute paths to uploaded rasters


def get_gdb_path() -> str:
    return _active_gdb


def list_layers(gdb_path: str) -> list:
    try:
        return fiona.listlayers(gdb_path)
    except Exception:
        return []


def load_layer(gdb_path: str, layer: str) -> gpd.GeoDataFrame:
    key = f"{gdb_path}::{layer}"
    if key not in _layer_cache:
        gdf = gpd.read_file(gdb_path, layer=layer)
        if gdf.crs and gdf.crs.to_epsg() != 4326:
            gdf = gdf.to_crs(epsg=4326)
        _layer_cache[key] = gdf
    return _layer_cache[key]


def raster_to_png_base64(raster_path: str, band: int = 1) -> dict:
    """Convert a raster band to a PNG tile and return bounds + base64 PNG."""
    try:
        import rasterio
        from rasterio.warp import calculate_default_transform, reproject, Resampling
        from rasterio.crs import CRS
        import PIL.Image

        with rasterio.open(raster_path) as src:
            # Reproject to WGS84 if needed
            dst_crs = CRS.from_epsg(4326)
            transform, width, height = calculate_default_transform(
                src.crs, dst_crs, src.width, src.height, *src.bounds
            )
            # Cap resolution for web display
            max_dim = 1024
            scale = max(width / max_dim, height / max_dim, 1)
            width = int(width / scale)
            height = int(height / scale)
            transform, _, _ = calculate_default_transform(
                src.crs, dst_crs, src.width, src.height, *src.bounds,
                dst_width=width, dst_height=height
            )

            kwargs = src.meta.copy()
            kwargs.update({
                "crs": dst_crs,
                "transform": transform,
                "width": width,
                "height": height,
                "driver": "MEM",
            })

            # Use in-memory rasterio for reprojection
            with rasterio.MemoryFile() as memfile:
                with memfile.open(**kwargs) as dst:
                    for i in range(1, src.count + 1):
                        reproject(
                            source=rasterio.band(src, i),
                            destination=rasterio.band(dst, i),
                            src_transform=src.transform,
                            src_crs=src.crs,
                            dst_transform=transform,
                            dst_crs=dst_crs,
                            resampling=Resampling.nearest,
                        )

                    # Build bounds
                    left, bottom, right, top = rasterio.transform.array_bounds(
                        height, width, transform
                    )

                    # Read band(s)
                    count = dst.count
                    if count >= 3:
                        r = dst.read(1).astype(float)
                        g = dst.read(2).astype(float)
                        b = dst.read(3).astype(float)
                        nodata = dst.nodata
                        alpha = None
                        if nodata is not None:
                            mask = (r == nodata) | (g == nodata) | (b == nodata)
                        else:
                            mask = (r == 0) & (g == 0) & (b == 0)
                        
                        def norm(a):
                            mn, mx = np.nanpercentile(a[~mask] if mask.any() else a, [2, 98])
                            if mx == mn:
                                return np.zeros_like(a, dtype=np.uint8)
                            return np.clip((a - mn) / (mx - mn) * 255, 0, 255).astype(np.uint8)

                        r8, g8, b8 = norm(r), norm(g), norm(b)
                        a8 = (~mask).astype(np.uint8) * 255
                        rgba = np.stack([r8, g8, b8, a8], axis=-1)
                    else:
                        data = dst.read(min(band, count)).astype(float)
                        nodata = dst.nodata
                        if nodata is not None:
                            mask = data == nodata
                        else:
                            mask = np.isnan(data)
                        valid = data[~mask]
                        if len(valid) == 0:
                            return {"error": "Raster has no valid data"}
                        mn, mx = np.percentile(valid, [2, 98])
                        if mx == mn:
                            norm_data = np.zeros_like(data, dtype=np.uint8)
                        else:
                            norm_data = np.clip((data - mn) / (mx - mn) * 255, 0, 255).astype(np.uint8)
                        
                        # Apply a colormap (viridis-like)
                        cmap = np.array([
                            [68, 1, 84], [59, 82, 139], [33, 145, 140],
                            [94, 201, 98], [253, 231, 37]
                        ], dtype=np.uint8)
                        indices = (norm_data / 255 * (len(cmap) - 1)).astype(int)
                        indices = np.clip(indices, 0, len(cmap) - 1)
                        r8 = cmap[indices, 0]
                        g8 = cmap[indices, 1]
                        b8 = cmap[indices, 2]
                        a8 = (~mask).astype(np.uint8) * 255
                        rgba = np.stack([r8, g8, b8, a8], axis=-1)

            img = PIL.Image.fromarray(rgba, mode="RGBA")
            buf = io.BytesIO()
            img.save(buf, format="PNG")
            buf.seek(0)
            b64 = base64.b64encode(buf.read()).decode()

            return {
                "base64": b64,
                "bounds": [[bottom, left], [top, right]],
                "width": width,
                "height": height,
                "bands": count,
            }

    except ImportError:
        return {"error": "rasterio or Pillow not installed"}
    except Exception as e:
        return {"error": str(e)}


def scan_rasters_in_folder(folder: str) -> list:
    """Return list of real raster files in a folder (non-recursive, excludes GDB internals)."""
    # Only true raster formats — NOT .jpg/.png which are rarely georeferenced
    exts = {".tif", ".tiff", ".img", ".vrt", ".nc", ".adf", ".ecw", ".jp2"}
    result = []
    try:
        p = Path(folder)
        # Non-recursive first: look in the folder itself
        for f in p.iterdir():
            if f.is_file() and f.suffix.lower() in exts:
                # Skip files inside a .gdb folder (those are GDB internals, not rasters)
                if ".gdb" not in str(f).lower():
                    result.append(str(f))
        # Also look one level deep in subfolders (but not inside .gdb)
        for sub in p.iterdir():
            if sub.is_dir() and sub.suffix.lower() != ".gdb":
                for f in sub.iterdir():
                    if f.is_file() and f.suffix.lower() in exts:
                        result.append(str(f))
    except Exception:
        pass
    return result


def find_rasters_near_gdb(gdb_path: str) -> list:
    """Look for rasters in the same folder as the GDB and one level up."""
    gdb_parent = Path(gdb_path).parent
    found = scan_rasters_in_folder(str(gdb_parent))
    # Also check the grandparent folder (one level up), in case GDB is in a subfolder
    grandparent = gdb_parent.parent
    if grandparent != gdb_parent:
        found += [r for r in scan_rasters_in_folder(str(grandparent)) if r not in found]
    return found


# ── Routes ────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/set-gdb", methods=["POST"])
def set_gdb():
    global _active_gdb, _layer_cache, _raster_files
    data = request.json or {}
    path = data.get("path", "").strip()
    if not path or not os.path.exists(path):
        return jsonify({"error": "Path does not exist"}), 400
    _active_gdb = path
    _layer_cache.clear()
    _raster_cache.clear()
    layers = list_layers(path)
    rasters = find_rasters_near_gdb(path)
    _raster_files = rasters
    return jsonify({
        "layers": layers,
        "path": path,
        "rasters": [{"name": Path(r).name, "path": r} for r in rasters]
    })


@app.route("/api/layers")
def get_layers():
    gdb = get_gdb_path()
    if not gdb:
        return jsonify({"layers": [], "gdb": "", "rasters": []})
    rasters = find_rasters_near_gdb(gdb) if gdb else []
    return jsonify({
        "layers": list_layers(gdb),
        "gdb": gdb,
        "rasters": [{"name": Path(r).name, "path": r} for r in rasters]
    })



@app.route("/api/scan-folder", methods=["POST"])
def scan_folder():
    """Scan any arbitrary folder for rasters."""
    data = request.json or {}
    folder = data.get("folder", "").strip()
    if not folder or not os.path.exists(folder):
        return jsonify({"error": "Folder does not exist"}), 400
    found = scan_rasters_in_folder(folder)
    return jsonify({
        "rasters": [{"name": Path(r).name, "path": r} for r in found],
        "scanned": folder,
        "count": len(found)
    })


@app.route("/api/debug/rasters")
def debug_rasters():
    """Debug endpoint: show what rasters are visible to the server."""
    gdb = get_gdb_path()
    found = find_rasters_near_gdb(gdb) if gdb else []
    gdb_parent = str(Path(gdb).parent) if gdb else ""
    folder_contents = []
    try:
        folder_contents = [str(f) for f in Path(gdb_parent).iterdir()] if gdb_parent else []
    except Exception:
        pass
    return jsonify({
        "active_gdb": gdb,
        "gdb_parent_folder": gdb_parent,
        "rasters_found_auto": found,
        "folder_contents": folder_contents,
        "cached_raster_files": _raster_files,
    })


@app.route("/api/layer/<path:layer_name>/geojson")
def layer_geojson(layer_name):
    gdb = get_gdb_path()
    if not gdb:
        abort(400)
    try:
        gdf = load_layer(gdb, layer_name)
        gdf2 = gdf.copy()
        for col in gdf2.columns:
            if col == "geometry":
                continue
            try:
                gdf2[col] = gdf2[col].astype(str)
            except Exception:
                gdf2 = gdf2.drop(columns=[col])
        return jsonify(json.loads(gdf2.to_json()))
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/layer/<path:layer_name>/attributes")
def layer_attributes(layer_name):
    gdb = get_gdb_path()
    if not gdb:
        abort(400)
    try:
        gdf = load_layer(gdb, layer_name)
        df = gdf.drop(columns="geometry", errors="ignore")
        cols = list(df.columns)
        rows = df.head(500).astype(str).values.tolist()
        return jsonify({"columns": cols, "rows": rows, "total": len(df)})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/layer/<path:layer_name>/download")
def layer_download(layer_name):
    fmt = request.args.get("format", "geojson").lower()
    gdb = get_gdb_path()
    if not gdb:
        abort(400)
    try:
        gdf = load_layer(gdb, layer_name)
        safe_name = layer_name.replace("/", "_").replace(" ", "_")

        if fmt == "geojson":
            buf = io.BytesIO(gdf.to_json().encode())
            buf.seek(0)
            return send_file(buf, as_attachment=True,
                             download_name=f"{safe_name}.geojson",
                             mimetype="application/geo+json")
        elif fmt == "csv":
            df = gdf.drop(columns="geometry", errors="ignore")
            buf = io.BytesIO(df.to_csv(index=False).encode())
            buf.seek(0)
            return send_file(buf, as_attachment=True,
                             download_name=f"{safe_name}.csv",
                             mimetype="text/csv")
        elif fmt == "shapefile":
            with tempfile.TemporaryDirectory() as tmp:
                shp_path = os.path.join(tmp, safe_name + ".shp")
                gdf.to_file(shp_path)
                zip_buf = io.BytesIO()
                with zipfile.ZipFile(zip_buf, "w", zipfile.ZIP_DEFLATED) as zf:
                    for f in Path(tmp).iterdir():
                        zf.write(f, f.name)
                zip_buf.seek(0)
                return send_file(zip_buf, as_attachment=True,
                                 download_name=f"{safe_name}.zip",
                                 mimetype="application/zip")
        else:
            return jsonify({"error": "Unknown format"}), 400
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ── Raster endpoints ───────────────────────────────────────────────────────────

@app.route("/api/raster/preview", methods=["POST"])
def raster_preview():
    """Return base64 PNG + bounds for a raster file path."""
    data = request.json or {}
    path = data.get("path", "").strip()
    band = int(data.get("band", 1))

    if not path or not os.path.exists(path):
        return jsonify({"error": "Raster file not found"}), 400

    cache_key = f"{path}::{band}"
    if cache_key not in _raster_cache:
        result = raster_to_png_base64(path, band)
        if "error" not in result:
            _raster_cache[cache_key] = result
        else:
            return jsonify(result), 500

    return jsonify(_raster_cache[cache_key])


@app.route("/api/upload-raster", methods=["POST"])
def upload_raster():
    """Accept a raster file upload."""
    global _raster_files
    if "file" not in request.files:
        return jsonify({"error": "No file provided"}), 400
    f = request.files["file"]
    if not f.filename:
        return jsonify({"error": "Empty filename"}), 400
    allowed = {".tif", ".tiff", ".img", ".nc", ".vrt"}
    ext = Path(f.filename).suffix.lower()
    if ext not in allowed:
        return jsonify({"error": f"Unsupported format: {ext}"}), 400
    dest = os.path.join(UPLOAD_FOLDER, f.filename)
    f.save(dest)
    if dest not in _raster_files:
        _raster_files.append(dest)
    return jsonify({"name": f.filename, "path": dest})


if __name__ == "__main__":
    app.run(debug=True, port=5050)
