# GeoPortal — Deployable GIS Web App

A Flask-based web GIS portal for viewing vector layers (GDB) and raster imagery in a browser.
Now supports raster overlays (.tif, .img, etc.) with opacity control and automatic colormap rendering.

---

## Features

- **Load any FileGDB** by pasting its path
- **Vector layers** — toggle, style by attribute, download as GeoJSON / Shapefile / CSV
- **Raster layers** — display GeoTIFFs and other rasters as map overlays with opacity slider
  - Single-band rasters use a viridis colormap automatically
  - Multi-band (RGB) rasters display true-color
  - Upload rasters directly from the browser
  - Auto-detects rasters in the same folder as the GDB
- **4 basemaps** — Dark, Street, Satellite, Topo
- **7 color palettes** including Rwanda theme

---

## Local Setup

```bash
# 1. Create virtual environment
python -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate

# 2. Install dependencies
pip install -r requirements.txt

# 3. Run locally
python app.py
# → open http://localhost:5050
```

---

## Deploy to Render.com (Free, Shareable Link)

> Render gives you a permanent URL like `https://geoportal-xxxx.onrender.com`

### Steps:

1. **Push to GitHub**
   ```bash
   git init
   git add .
   git commit -m "initial"
   git remote add origin https://github.com/YOUR_USERNAME/geoportal.git
   git push -u origin main
   ```

2. **Go to [render.com](https://render.com)** → New → Web Service

3. **Connect your GitHub repo**

4. Set these settings:
   | Field | Value |
   |-------|-------|
   | Environment | Python 3 |
   | Build Command | `pip install -r requirements.txt` |
   | Start Command | `gunicorn app:app --bind 0.0.0.0:$PORT --workers 2 --timeout 120` |
   | Plan | Free |

5. Click **Deploy** — Render gives you a live URL to share!

> ⚠️ **Important**: On cloud deployment, GDB paths like `C:\Users\...` won't work.
> Upload your GDB as a zip, or host your data files — see "Hosting Data" below.

---

## Deploy to Railway.app (Alternative)

1. Push to GitHub (same as above)
2. Go to [railway.app](https://railway.app) → New Project → Deploy from GitHub
3. Select your repo — Railway auto-detects the `railway.json` config
4. Get your live URL from the Railway dashboard

---

## Hosting Your GDB Data on the Cloud

Since your GDB lives on your local machine, you need to host it for cloud deployments.
Two easy options:

### Option A — Bundle data in the repo (small datasets only)
```
geoportal/
  data/
    G7 Kigarama.gdb/   ← copy the GDB folder here
  app.py
  ...
```
Then set the default path in `app.py`:
```python
DEFAULT_GDB = os.path.join(os.path.dirname(__file__), "data", "G7 Kigarama.gdb")
```

### Option B — Cloud storage (large datasets)
1. Upload your GDB to **Google Drive** or **Dropbox**
2. Download it on startup in `app.py` using `requests` and the public share link
3. Cache it in a `/tmp` folder

---

## Raster Support Details

| Format | Supported |
|--------|-----------|
| GeoTIFF (.tif, .tiff) | ✅ |
| ERDAS Imagine (.img) | ✅ |
| NetCDF (.nc) | ✅ |
| VRT (.vrt) | ✅ |
| JPEG/PNG (georeferenced) | ✅ |

Rasters are:
- Reprojected to WGS84 automatically
- Resampled to max 1024×1024 for browser performance
- Single-band: viridis colormap (2nd–98th percentile stretch)
- Multi-band: true-color RGB composite

---

## File Structure

```
geoportal/
├── app.py              ← Flask backend
├── requirements.txt    ← Python dependencies
├── Procfile            ← For Heroku / Render
├── render.yaml         ← One-click Render config
├── railway.json        ← Railway config
├── README.md           ← This file
├── templates/
│   └── index.html      ← Frontend UI
└── uploads/            ← Uploaded rasters stored here
```
