"""The selection of a page shown: which strokes and images are picked, where they are
under the pending transform, and what the selection box shows meanwhile.

It works on the page as parsed for drawing (viewer.rm_parser.PageScene), whose items
carry the same ids as the page's file with its pending edits, so the ids picked here
are those the commit (LibrarySource.transform_items) works on. Strokes of a group
anchored to typed text are stored relative to the anchor position; they are picked and
drawn where the page shows them (stored + the renderer's offset), and that offset is
handed to the commit, which conjugates the transform with it.

How the app picks (viewer.rm_select): a selection rectangle takes a stroke when any of
its points lies in it, an image when it meets the image's box; Shift toggles; a lasso
takes a stroke only when all its points lie in the lasso.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace

from viewer import rm_select as rsel
from viewer import rm_stroke as rs
from viewer.rm_parser import ImageItem, Layer, PageScene, Point, Stroke


@dataclass
class Picked:
    """A selectable item of a layer: a stroke (with the offset the page draws it at) or
    an image."""

    item: tuple[int, int]
    stroke: Stroke | None = None
    image: ImageItem | None = None
    offset: tuple[float, float] = (0.0, 0.0)

    def points(self) -> tuple[list[float], list[float]]:
        dx, dy = self.offset
        if self.stroke is not None:
            return [p.x + dx for p in self.stroke.points], [p.y + dy for p in self.stroke.points]
        return [v[0] for v in self.image.vertices], [v[1] for v in self.image.vertices]

    def box(self, m: rsel.Matrix = rsel.IDENTITY) -> rsel.Box | None:
        """Its box in scene coordinates after `m`: a stroke's points widened by half of
        what its tool reaches at its (transformed) size (Line::boundingRect), an image's
        corners."""
        xs, ys = self.points()
        if not xs:
            return None
        if not rsel.is_identity(m):
            mapped = [rsel.map_point(m, x, y) for x, y in zip(xs, ys)]
            xs, ys = [p[0] for p in mapped], [p[1] for p in mapped]
        left, top, right, bottom = min(xs), min(ys), max(xs), max(ys)
        if self.stroke is None:
            return left, top, right, bottom
        pad = rs._reach(self.stroke.tool_id, self.stroke.thickness_scale * rsel.scale_of(m)) * 0.5
        return left - pad, top - pad, right + pad, bottom + pad


def selectable(scene: PageScene | None, layer: int, offsets: dict | None = None) -> list[Picked]:
    """The items of a layer a selection can take, bottom to top: strokes of drawing
    tools and the legacy eraser (0x1405b7530), and placed images. `offsets`: the offset
    of each anchored stroke (by id(stroke), as renderer.stroke_offsets gives them)."""
    if scene is None or not 0 <= layer < len(scene.layers):
        return []
    out = []
    for item in scene.layers[layer].items():
        if isinstance(item, ImageItem):
            if item.vertices and item.item_id is not None:
                out.append(Picked(tuple(item.item_id), image=item))
        elif item.item_id is not None and item.points and item.tool_id not in rsel.NOT_SELECTABLE:
            out.append(Picked(tuple(item.item_id), stroke=item, offset=(offsets or {}).get(id(item), (0.0, 0.0))))
    return out


def pick_rect(items: list[Picked], rect: tuple[int, int, int, int]) -> list[tuple[int, int]]:
    """The items a selection rectangle (whole units: x, y, w, h) takes."""
    out = []
    for p in items:
        if p.stroke is not None:
            xs, ys = p.points()
            if rsel.rect_hits_line(xs, ys, rect):
                out.append(p.item)
        elif rsel.rect_hits_image(p.image.vertices, rect):
            out.append(p.item)
    return out


def pick_lasso(items: list[Picked], points: list[tuple[float, float]]) -> list[tuple[int, int]]:
    """The items a lasso through scene `points` takes."""
    if len(points) < 3:
        return []
    lasso = rsel.Lasso(points)
    out = []
    for p in items:
        if p.stroke is not None:
            xs, ys = p.points()
            if lasso.holds_line(xs, ys, p.box()):
                out.append(p.item)
        elif lasso.holds_image(p.image.vertices):
            out.append(p.item)
    return out


def toggled(current: list[tuple[int, int]], hit: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """ToggleSelection: each item hit flips in or out (0x1408c9250: was XOR hit)."""
    hit_set = set(hit)
    kept = [i for i in current if i not in hit_set]
    return kept + [i for i in hit if i not in set(current)]


@dataclass
class Selection:
    """What is selected on a page shown, and the transform pending for it (the
    SceneController's QTransform at +0x2c8, multiplied by every move, scale and turn
    until the selection closes)."""

    layer: int
    items: list[tuple[int, int]]
    pending: rsel.Matrix = rsel.IDENTITY
    merge: object = None                 # the paste's undo step, when this is what it pasted
    picked: dict = field(default_factory=dict)      # id -> Picked, as last loaded

    def chosen(self) -> list[Picked]:
        return [self.picked[i] for i in self.items if i in self.picked]

    def box(self, with_pending: bool = True) -> rsel.Box | None:
        m = self.pending if with_pending else rsel.IDENTITY
        return rsel.union(p.box(m) for p in self.chosen())

    def apply(self, m: rsel.Matrix) -> None:
        self.pending = rsel.multiply(self.pending, m)

    @property
    def has_stroke(self) -> bool:
        return any(p.stroke is not None for p in self.chosen())

    @property
    def has_image(self) -> bool:
        return any(p.image is not None for p in self.chosen())

    def image_keys(self) -> list[bytes]:
        return [p.image.image_ref for p in self.chosen() if p.image is not None]

    def offsets(self) -> dict:
        """The offset of each selected anchored stroke (item id -> (x, y))."""
        return {p.item: p.offset for p in self.chosen() if p.stroke is not None and p.offset != (0.0, 0.0)}


def preview_scene(scene: PageScene, items: list[Picked]) -> PageScene:
    """A page holding only `items` (strokes moved to where the page draws them), to draw
    in the selection box."""
    strokes, images = [], []
    for p in items:
        if p.stroke is not None:
            dx, dy = p.offset
            points = [Point(q.x + dx, q.y + dy, q.speed, q.direction, q.width, q.pressure) for q in p.stroke.points] \
                if (dx or dy) else p.stroke.points
            strokes.append(replace(p.stroke, points=points, anchor=None))
        else:
            images.append(p.image)
    return replace(scene, layers=[Layer(name="selection", strokes=strokes, images=images)], text=None, images=images)


def without(scene: PageScene, items) -> PageScene:
    """The page with `items` left out (the selection box shows them)."""
    gone = set(items)
    layers = [replace(layer, strokes=[s for s in layer.strokes if s.item_id not in gone],
                      images=[i for i in layer.images if i.item_id not in gone]) for layer in scene.layers]
    return replace(scene, layers=layers)


def line_rects(items: list[Picked], m: rsel.Matrix = rsel.IDENTITY) -> list[tuple[int, int, int, int]]:
    """The aligned box of each stroke (x, y, w, h), as DocumentViewport outlines them."""
    import math
    out = []
    for p in items:
        if p.stroke is None:
            continue
        box = p.box(m)
        if box is not None:
            left, top = math.floor(box[0]), math.floor(box[1])
            out.append((left, top, math.ceil(box[2]) - left, math.ceil(box[3]) - top))
    return out
