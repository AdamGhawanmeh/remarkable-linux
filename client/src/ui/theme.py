"""Design tokens taken from the official desktop client's QML (ARCHITECTURE.md §2).

Sources (all under resources/):
  qml/client/Colors.qml            client palette (gray1..gray6, light, dark, ...)
  qml/common/Values.qml            layout metrics; desktop builds use `scale: 0.5`
  ark-imports/ark/tokens/*.qml     Ark design tokens (typography, sidebar, focus)
Values below are the QML numbers already multiplied by the desktop scale.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from PySide6.QtCore import QRectF, QSize, Qt
from PySide6.QtGui import QColor, QFont, QIcon, QImage, QPainter, QPixmap
from PySide6.QtSvg import QSvgRenderer

ICON_DIR = Path(__file__).resolve().parent.parent / "assets" / "icons" / "icons"

SCALE = 0.5   # Values.qml: `readonly property real scale: 0.5` on desktop


class Colors:
    # Colors.qml
    black = "#000000"
    gray1 = "#37322F"          # foreground 6 (text)
    gray2 = "#6E635E"          # foreground 1
    gray3 = "#E0DACC"          # background 6 (thumbnail border, hover)
    gray4 = "#ECE6DA"          # background 4 (folder tile, thumbnail placeholder)
    gray5 = "#D2CABC"          # background 8
    gray6 = "#EDE8DE"          # background 3
    white = "#FFFFFF"
    dark = "#37322F"           # selected folder tile
    dark2 = "#4D4542"
    light = "#F9F6F1"          # background 2 (window / sidebar fill)
    active_button = "#D9D2C4"
    # ark tokens (Style.qml / Sidebar.qml)
    divider = "#D2CABC"
    hover = "#E7E1D5"
    selected = "#E0DACC"       # sidebar selected item fill
    text = "#211E1C"           # primary text
    text_secondary = "#584F4B"
    focus = "#1142D4"
    # Values.qml
    accent = "#E24A37"


class Metrics:
    base_margin = 8                 # baseMargin 16
    content_margin = 16             # contentMargin
    navigator_margin = 21           # navigatorMargin 42
    sidebar_width = 175             # navigatorSidebarWidth 350
    sidebar_item_height = 40        # iconSizeSidebarMenu 40 → 20 px icon in a 40 px row
    sidebar_icon = 20
    header_height = 64              # navigationHeaderBarHeight 128
    entries_header_height = 40      # navigationEntriesHeaderHeight 80
    folder_item_height = 40         # navigatorFolderItemHeight 80
    folder_item_width = 163         # navigatorFolderItemWidth 327
    folder_inner_margin = 11        # navigatorFolderItemInnerMargin 22
    folder_radius = 2               # navigatiorFolderItemRadius 4
    grid_item_width = 180           # navigatorContentGridItemWidth 360
    grid_item_margin = 8            # navigatorContentGridItemMargin
    grid_bottom_height = 16 + 12 + 20   # navigatorGridBottomItemHeight
    list_item_height = 80           # navigatorContentListItemHeight 160
    thumbnail_border = 2            # cover.borderWidth
    icon_small = 16                 # iconSizeSmall 32
    icon_medium = 24                # iconSizeMedium 48
    icon_large = 32                 # iconSizeLarge 64
    radius = 4                      # rectangleRadius 8
    page_aspect = 1872 / 1404       # aspect_Device


class Fonts:
    families = ["reMarkable Sans", "Noto Sans", "sans-serif"]   # ark tokens fontFamily
    title = 20          # navigatorTitleFontSize 40
    large = 16          # fontSizeLarge 32 (grid item title)
    item = 15           # navigatorItemFontSize 30
    medium = 14         # fontSizeMedium 28 (folder label, subtext)
    small = 12          # fontSizeSmall 24 / caption.md


def font(px: int, weight: QFont.Weight = QFont.Weight.Normal) -> QFont:
    f = QFont()
    f.setFamilies(Fonts.families)
    f.setPixelSize(px)
    f.setWeight(weight)
    return f


def icon_path(name: str) -> Path:
    return ICON_DIR / name


@lru_cache(maxsize=512)
def icon_image(name: str, color: str, size: int, device_ratio: float = 2.0) -> QImage:
    """Render an Ark SVG icon tinted with `color` (icons ship as black shapes)."""
    px = max(1, round(size * device_ratio))
    image = QImage(px, px, QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(Qt.GlobalColor.transparent)
    renderer = QSvgRenderer(str(icon_path(name)))
    painter = QPainter(image)
    renderer.render(painter, QRectF(0, 0, px, px))
    painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceIn)
    painter.fillRect(image.rect(), QColor(color))
    painter.end()
    image.setDevicePixelRatio(device_ratio)
    return image


def icon(name: str, color: str = Colors.gray1, size: int = Metrics.icon_medium) -> QIcon:
    qicon = QIcon()
    qicon.addPixmap(QPixmap.fromImage(icon_image(name, color, size)))
    return qicon


def icon_size(size: int = Metrics.icon_medium) -> QSize:
    return QSize(size, size)


STYLESHEET = f"""
QWidget {{ background: {Colors.light}; color: {Colors.text}; }}
QToolTip {{ background: {Colors.dark}; color: {Colors.light}; border: none; padding: 4px 8px; }}
QLineEdit {{
    background: {Colors.white}; border: 1px solid {Colors.divider}; border-radius: {Metrics.radius}px;
    padding: 4px 8px; selection-background-color: {Colors.focus};
}}
QLineEdit:focus {{ border: 2px solid {Colors.focus}; }}
QToolButton {{ border: none; border-radius: {Metrics.radius}px; padding: 6px; background: transparent; }}
QToolButton:hover {{ background: {Colors.hover}; }}
QToolButton:pressed, QToolButton:checked {{ background: {Colors.active_button}; }}
QToolButton:disabled {{ color: {Colors.gray5}; }}
QMenu {{ background: {Colors.light}; border: 1px solid {Colors.divider}; padding: 4px; }}
QMenu::item {{ padding: 6px 24px 6px 12px; border-radius: 2px; }}
QMenu::item:selected {{ background: {Colors.hover}; }}
QScrollBar:vertical {{ background: transparent; width: 10px; }}
QScrollBar::handle:vertical {{ background: {Colors.gray4}; border-radius: 5px; min-height: 25px; }}
QScrollBar::handle:vertical:pressed {{ background: {Colors.gray5}; }}
QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; width: 0; }}
QScrollBar:horizontal {{ background: transparent; height: 10px; }}
QScrollBar::handle:horizontal {{ background: {Colors.gray4}; border-radius: 5px; min-width: 25px; }}
QStatusBar {{ background: {Colors.light}; color: {Colors.text_secondary}; border-top: 1px solid {Colors.divider}; }}
"""
