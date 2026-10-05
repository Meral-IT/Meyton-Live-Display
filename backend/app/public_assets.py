"""The same read-only frontend assets for SFTP publication and manual exports."""

from pathlib import Path
import re


def public_assets(iframe_origins=""):
    root = Path(__file__).resolve().parents[1]
    if not (root / "frontend/public").is_dir():
        root = root.parent
    source = root / "frontend/public"
    htaccess = (root / "hosting/.htaccess").read_text()
    htaccess, count = re.subn(r"frame-ancestors[^;\"]*", "frame-ancestors 'self'" + (" " + iframe_origins if iframe_origins else ""), htaccess)
    if count != 1:
        raise ValueError("Public .htaccess must contain one frame-ancestors directive")
    files = {".htaccess": htaccess.encode()}
    for name in ("app.js", "publication.js", "target.js", "branding.js", "reload.js", "style.css"):
        files[name] = (source / name).read_bytes()
    for path in sorted((source / "vendor").rglob("*")):
        if path.is_file():
            files[path.relative_to(source).as_posix()] = path.read_bytes()
    html = (source / "index.html").read_text()
    html = html.replace('<meta charset="utf-8">', '<meta charset="utf-8">\n  <meta name="meyton-public" content="1">')
    html = html.replace('      <a class="button secondary" href="/admin">Profile bearbeiten</a>\n', '')
    files["index.html"] = html.encode()
    return files
