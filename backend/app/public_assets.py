"""The same read-only frontend assets for SFTP publication and manual exports."""

from pathlib import Path


def public_assets():
    root = Path(__file__).resolve().parents[1]
    if not (root / "frontend/public").is_dir():
        root = root.parent
    source = root / "frontend/public"
    files = {".htaccess": (root / "hosting/.htaccess").read_bytes()}
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
