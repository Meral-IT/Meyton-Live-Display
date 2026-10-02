"""Sponsor images on the existing configuration volume; no image dependencies."""

import base64
import os
import re
import secrets
import struct
import tempfile
import threading
from pathlib import Path
from urllib.parse import quote
from xml.etree import ElementTree as ET

MAX_IMAGE_BYTES = 5 * 1024 * 1024
MAX_REQUEST_BYTES = MAX_IMAGE_BYTES * 4 // 3 + 2048
IMAGE_POLICY = "sandbox; default-src 'none'; style-src 'unsafe-inline'; img-src data:; base-uri 'none'"
TYPES = {"png": "image/png", "jpg": "image/jpeg", "svg": "image/svg+xml"}


def image_data(encoded):
    try:
        data = base64.b64decode(encoded, validate=True)
    except (ValueError, TypeError) as error:
        raise ValueError("Bilddaten ungültig") from error
    if not data or len(data) > MAX_IMAGE_BYTES:
        raise ValueError("Bilder dürfen höchstens 5 MiB groß sein")
    # ponytail: raster headers checked here; browser decoding rejects corrupt images.
    # Add a full image decoder if uploads need corruption repair or conversion.
    if data.startswith(b"\x89PNG\r\n\x1a\n") and data[12:16] == b"IHDR" and data.endswith(b"IEND\xaeB`\x82"):
        width, height = struct.unpack(">II", data[16:24])
        if not width or not height or width * height > 40_000_000:
            raise ValueError("PNG-Abmessungen ungültig oder zu groß")
        return data, "png"
    if data.startswith(b"\xff\xd8\xff") and data.endswith(b"\xff\xd9"):
        return data, "jpg"
    check = data.replace(b"\x00", b"").upper()
    if b"<!DOCTYPE" in check or b"<!ENTITY" in check:
        raise ValueError("SVG-DTD und Entitäten sind nicht erlaubt")
    try:
        root = ET.fromstring(data)
    except ET.ParseError as error:
        raise ValueError("Nur PNG, JPEG und SVG sind erlaubt") from error
    if root.tag not in ("svg", "{http://www.w3.org/2000/svg}svg"):
        raise ValueError("SVG-Wurzelelement fehlt")
    for node in root.iter():
        if node.tag.split("}")[-1].lower() in ("script", "foreignobject", "iframe", "object", "embed"):
            raise ValueError("Aktive SVG-Inhalte sind nicht erlaubt")
        for key, value in node.attrib.items():
            key = key.split("}")[-1].lower()
            if key.startswith("on") or "javascript:" in value.lower():
                raise ValueError("Aktive SVG-Inhalte sind nicht erlaubt")
            if key == "href" and not value.startswith(("#", "data:image/png;base64,", "data:image/jpeg;base64,")):
                raise ValueError("Externe SVG-Verweise sind nicht erlaubt")
    return ET.tostring(root, encoding="utf-8", xml_declaration=True), "svg"


class SponsorStore:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.images = self.list()
        self.lock = threading.Lock()

    def path(self, image_id):
        if not re.fullmatch(r"[0-9a-f]{16}_[\w .-]{1,80}\.(png|jpg|svg)", image_id):
            raise FileNotFoundError(image_id)
        path = self.directory / image_id
        if not path.is_file():
            raise FileNotFoundError(image_id)
        return path

    def list(self):
        images = []
        for path in sorted(self.directory.iterdir()):
            try:
                self.path(path.name)
            except FileNotFoundError:
                continue
            text_path = path.with_name(path.name + ".txt")
            images.append({"id": path.name, "name": path.name.split("_", 1)[1],
                           "text": text_path.read_text(encoding="utf-8") if text_path.exists() else "",
                           "url": "/api/sponsors/" + quote(path.name)})
        return images

    def write_file(self, path, data):
        descriptor, temporary = tempfile.mkstemp(dir=self.directory, prefix=".upload-")
        try:
            with os.fdopen(descriptor, "wb") as file:
                file.write(data)
                file.flush()
                os.fsync(file.fileno())
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def add(self, name, encoded):
        with self.lock:
            if not isinstance(name, str) or not name.strip() or len(name) > 128:
                raise ValueError("Bildname ungültig")
            if len(self.images) >= 64:
                raise ValueError("Maximal 64 Sponsorenbilder")
            data, extension = image_data(encoded)
            stem = re.sub(r"[^\w .-]", "_", Path(name.replace("\\", "/")).stem)[:80] or "Sponsor"
            image_id = secrets.token_hex(8) + "_" + stem + "." + extension
            self.write_file(self.directory / image_id, data)
            self.images = self.list()
            return next(image for image in self.images if image["id"] == image_id)

    def set_text(self, image_id, text):
        if not isinstance(text, str) or len(text) > 300:
            raise ValueError("Sponsorentext darf höchstens 300 Zeichen lang sein")
        with self.lock:
            path = self.path(image_id)
            self.write_file(path.with_name(path.name + ".txt"), text.strip().encode("utf-8"))
            self.images = self.list()
            return next(image for image in self.images if image["id"] == image_id)

    def delete(self, image_id):
        with self.lock:
            self.path(image_id).unlink()
            (self.directory / (image_id + ".txt")).unlink(missing_ok=True)
            self.images = self.list()
