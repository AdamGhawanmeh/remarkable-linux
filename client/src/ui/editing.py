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
FIT_TO_PAGE = False                     # the app places a picture at one unit a pixel
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


# -- the clipboard ----------------------------------------------------------------------
#
# Copying (SceneController.cloneSelectedItems, 0x14043a320) keeps clones of the selected
# items, scaled by the view's default scale, in the app's Clipboard.items; nothing goes to
# the system clipboard (the app writes that only for text, 0x140607560). Here a clone is a
# plain record QML can hold and hand back: where its picture is, its corners (scaled as
# the app's are), and the two timestamps a pasted copy keeps.

def clip_image(doc_id: str, page_id: str, image, scale: float) -> dict:
    """The clipboard record of a placed image, as cloneSelectedItems(layer, scale) makes
    a clone: no id, no moveId, its corners scaled by `scale` (about 0, 0)."""
    value = image.value
    from viewer import rm_writer
    decoded = rm_writer.decode_image_value(value)
    return {"type": "image", "documentId": doc_id, "pageId": page_id, "fileName": image.file_name,
            "vertices": [[x * scale, y * scale, u, v] for x, y, u, v in image.vertices],
            "boundsTimestamp": list(decoded.bounds_timestamp), "refTimestamp": list(decoded.ref_timestamp)}


def clip_images(items) -> list[dict]:
    """The image records among clipboard items (as QML hands them back), checked."""
    out = []
    for item in items or []:
        item = dict(item) if hasattr(item, "keys") else None
        if not item or item.get("type") != "image":
            continue
        try:
            corners = [tuple(float(c) for c in v) for v in item["vertices"]]
            stamps = tuple(tuple(int(n) for n in item[k]) for k in ("boundsTimestamp", "refTimestamp"))
            if len(corners) != 4 or any(len(c) != 4 for c in corners) or any(len(s) != 2 for s in stamps):
                continue
            out.append({**item, "vertices": corners, "timestamps": stamps,
                        "documentId": str(item["documentId"]), "pageId": str(item["pageId"]),
                        "fileName": str(item["fileName"])})
        except (KeyError, TypeError, ValueError):
            continue
    return out


def items_bounds(items) -> tuple[float, float, float, float] | None:
    """(x, y, width, height) around clipboard items (getItemBoundingRect)."""
    corners = [c for item in clip_images(items) for c in item["vertices"]]
    return corner_bounds(corners) if corners else None


def paste_position(items, scene_bounds: tuple[float, float, float, float], pos: tuple[float, float],
                   last_selection: tuple[float, float] | None) -> tuple[float, float]:
    """Where pasted items are centred (SceneController.getSelectionPastePos, 0x14043fb50):
    at `pos`, moved by half their size when `pos` is the corner of the area last
    selected, then moved back inside `scene_bounds` on the left, right and top (a page
    grows downwards)."""
    bounds = items_bounds(items)
    if bounds is None:
        return pos
    _, _, w, h = bounds
    x, y = pos
    if last_selection is not None and _fuzzy(x, last_selection[0]) and _fuzzy(y, last_selection[1]):
        x, y = x + w * 0.5, y + h * 0.5
    left, top = x - w * 0.5, y - h * 0.5
    bx, by, bw, bh = scene_bounds
    if bx <= left and left + w <= bx + bw and by <= top and top + h <= by + bh:
        return x, y
    dx = bx - left if left < bx else (bx + bw) - (left + w) if left + w > bx + bw else 0.0
    dy = by - top if top < by else 0.0
    return x + dx, y + dy


def _fuzzy(a: float, b: float) -> bool:
    """qFuzzyCompare."""
    return abs(a - b) * 1e12 <= min(abs(a), abs(b)) or a == b


def pasted_corners(item: dict, scale: float, center: tuple[float, float]) -> list:
    """A clipboard item's corners as cloneAddAndSelectItems(layer, items, scale, pos)
    places its copy: scaled by `scale` about 0, 0 (left as they are when the scale is 1,
    as qFuzzyCompare decides), then moved so the middle of their bounds is at `pos`
    (0x14043a060, 0x1409af310)."""
    corners = item["vertices"]
    if not _fuzzy(scale, 1.0):
        corners = [(x * scale, y * scale, u, v) for x, y, u, v in corners]
    x, y, w, h = corner_bounds(corners)
    return moved(corners, center[0] - x - w * 0.5, center[1] - y - h * 0.5)
