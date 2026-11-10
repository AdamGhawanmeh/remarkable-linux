"""Preparing what is put on a page here: an image file, as the app stores and places one.

reMarkable.exe 3.29 (SceneController::insertImageFileAsSceneItem, 0x140442280) reads the
file with EXIF rotation applied, and stores not the file but its own encoding of the
picture (0x140a4e6f0):
    - larger than 3200 x 3200, it is scaled down to fit, smoothly, keeping its shape;
    - with an alpha channel it is written as PNG, if that is at most 3 MiB;
    - otherwise it is flattened onto white and written as JPEG, at quality 90, 70, 50,
      30, then 20 until the file is at most 3 MiB.
The item is the stored picture at one scene unit a pixel, centred on the drop position
(0x14043c3c0, 0x1409af310), and stays selected so it can be resized at once.

This client cannot resize an item yet, so one difference: a picture larger than the page
is placed scaled down to fit the page (FIT_TO_PAGE). The stored file is the same.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QBuffer, QByteArray, QIODevice, QSize, Qt
from PySide6.QtGui import QImage, QImageReader, QImageWriter, QPainter

from core.edits import EditError
from viewer.rm_parser import PAGE_HEIGHT, PAGE_WIDTH

MAX_SIDE = 3200                         # pixels
MAX_BYTES = 0x300000                    # 3 MiB
JPEG_QUALITIES = (90, 70, 50, 30, 20)
FIT_TO_PAGE = True                      # until items can be resized here
# What DocumentViewport's drop area accepts (isSupportedImageUrl).
DROP_SUFFIXES = (".png", ".jpg", ".jpeg")


def read_image(path: str | Path) -> QImage:
    """The picture in a file, turned as its EXIF orientation says."""
    reader = QImageReader(str(path))
    reader.setAutoTransform(True)
    image = reader.read()
    if image.isNull():
        raise EditError(f"Could not read the image {Path(path).name}: {reader.errorString()}")
    return image


def _written(image: QImage, fmt: bytes, quality: int | None = None) -> bytes | None:
    data = QByteArray()
    buffer = QBuffer(data)
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    writer = QImageWriter(buffer, fmt)
    if quality is not None:
        writer.setQuality(quality)
    ok = writer.write(image)
    buffer.close()
    return bytes(data) if ok else None


def encode_image(image: QImage) -> tuple[bytes, str, QSize]:
    """(file, "png" or "jpg", size in pixels) of a picture as the app stores it."""
    if image.isNull():
        raise EditError("There is no image to insert")
    if image.width() > MAX_SIDE or image.height() > MAX_SIDE:
        image = image.scaled(QSize(MAX_SIDE, MAX_SIDE), Qt.AspectRatioMode.KeepAspectRatio,
                             Qt.TransformationMode.SmoothTransformation)
    if image.hasAlphaChannel():
        data = _written(image, b"PNG")
        if data is not None and len(data) <= MAX_BYTES:
            return data, "png", image.size()
        flat = QImage(image.size(), QImage.Format.Format_RGB32)
        flat.fill(Qt.GlobalColor.white)
        painter = QPainter(flat)
        painter.drawImage(0, 0, image)
        painter.end()
        image = flat
    for quality in JPEG_QUALITIES:
        data = _written(image, b"JPEG", quality)
        if data is not None and len(data) <= MAX_BYTES:
            return data, "jpg", image.size()
    raise EditError("The image is too large to insert")


def placed_size(width: int, height: int) -> tuple[float, float]:
    """The size, in scene units, a stored picture of this many pixels is placed at."""
    scale = min(1.0, PAGE_WIDTH / width, PAGE_HEIGHT / height) if FIT_TO_PAGE else 1.0
    return width * scale, height * scale


def corners(center_x: float, center_y: float, width: float, height: float) -> list[tuple[float, float, float, float]]:
    """An image item's four corners (x y u v) for a rectangle centred on a scene
    position: top left, top right, bottom right, bottom left, as the tablet lists them."""
    x0, y0 = center_x - width / 2, center_y - height / 2
    x1, y1 = x0 + width, y0 + height
    return [(x0, y0, 0.0, 0.0), (x1, y0, 1.0, 0.0), (x1, y1, 1.0, 1.0), (x0, y1, 0.0, 1.0)]


def prepare_image(path: str | Path, center_x: float, center_y: float) -> tuple[bytes, str, list]:
    """(file, extension, corners) for the image file `path` dropped at a scene position."""
    data, extension, size = encode_image(read_image(path))
    return data, extension, corners(center_x, center_y, *placed_size(size.width(), size.height()))


# -- selecting and transforming placed images -----------------------------------------

def _inside(vertices, x: float, y: float) -> bool:
    """Whether a scene point lies in the quadrilateral of an item's corners (convex: a
    rectangle, perhaps turned or mirrored)."""
    signs = set()
    for (x0, y0, *_), (x1, y1, *_) in zip(vertices, [*vertices[1:], vertices[0]]):
        cross = (x1 - x0) * (y - y0) - (y1 - y0) * (x - x0)
        if cross:
            signs.add(cross > 0)
    return len(signs) <= 1


def image_at(scene, layer_index: int, x: float, y: float):
    """The topmost placed image of the layer at a scene point (as the app's
    SceneController.selectImageAtPoint, which only looks in the layer given), or None."""
    if scene is None or not 0 <= layer_index < len(scene.layers):
        return None
    layer = scene.layers[layer_index]
    for item in reversed(layer.items()):
        if getattr(item, "image_ref", None) and len(item.vertices) == 4 and _inside(item.vertices, x, y):
            return item
    return None


def find_image(scene, key: bytes):
    """(layer index, image) of the live image showing `key`, or None."""
    for index, layer in enumerate(scene.layers if scene is not None else []):
        for image in layer.images:
            if image.image_ref == key and image.vertices:
                return index, image
    return None


def corner_bounds(vertices) -> tuple[float, float, float, float]:
    """(x, y, width, height) of the corners' bounding box."""
    xs, ys = [v[0] for v in vertices], [v[1] for v in vertices]
    return min(xs), min(ys), max(xs) - min(xs), max(ys) - min(ys)


def moved(vertices, dx: float, dy: float) -> list:
    return [(x + dx, y + dy, u, v) for x, y, u, v in vertices]


def scaled(vertices, ox: float, oy: float, x_scale: float, y_scale: float) -> list:
    """Scaled about a scene point, as SceneSelectionHandler's corner handles ask
    (scaleSelectedItems); a negative scale mirrors."""
    return [(ox + (x - ox) * x_scale, oy + (y - oy) * y_scale, u, v) for x, y, u, v in vertices]


def rotated(vertices, ox: float, oy: float, degrees: float) -> list:
    """Turned about a scene point, clockwise on screen for a positive angle, as the
    handler's rotation handle asks (rotateSelectedItems; QML rotation is clockwise)."""
    import math
    c, s = math.cos(math.radians(degrees)), math.sin(math.radians(degrees))
    return [(ox + (x - ox) * c - (y - oy) * s, oy + (x - ox) * s + (y - oy) * c, u, v) for x, y, u, v in vertices]
