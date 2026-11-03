"""Library navigator: sidebar, header rows and the grid/list of entries.

Structure follows qml/client/desktop/navigator/*.qml:
  DesktopSidebar  → Sidebar        (views from Sidebar.qml; Import runs the import dialog,
                                    Settings is shown disabled)
  ActionHeader    → ActionRow      (Back, Folder, Import / Empty trash, Screen Share, search, sync)
  ContentEntriesHeader → CrumbRow  (breadcrumbs with angle_right separators, Sort by, view)
  EntryGridView / ContentEntriesListItem → EntryView (folder tiles first, then documents)
  ContextMenu.qml → the right-click menu (Favorite, Rename, Move, trash / Restore, Delete)
  NamingDialog.qml, DocumentDropOverlay.qml → naming dialog, drop overlay texts

Changes are not made here: the navigator emits changeRequested(what, change, done) with
a callable on the SyncClient write API, and the main window runs it in the background.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from PySide6.QtCore import QPoint, QRect, QRectF, QSize, Qt, Signal
from PySide6.QtGui import (QColor, QContextMenuEvent, QDragEnterEvent, QDropEvent, QFont, QFontMetrics,
                           QImage, QMouseEvent, QPainter, QPaintEvent, QPen, QPixmap)
from PySide6.QtWidgets import (QAbstractScrollArea, QDialog, QDialogButtonBox, QFileDialog, QHBoxLayout,
                               QInputDialog, QLabel, QLineEdit, QMenu, QMessageBox, QSizePolicy,
                               QToolButton, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget)

from core.models import TRASH_ID, ROOT_ID, Document, DocumentTree, FileType, Node, unique_name
from core.sync import IMPORT_TYPES

from .library import SIDEBAR, Crumb, Entry, LibraryQuery, Location, SortOrder, ViewFilter
from .theme import Colors, Fonts, Metrics, font, icon, icon_image, icon_size
from .workers import ThumbnailService

GRID, LIST = "grid", "list"

# Values.qml emptyViewTitle/emptyViewDescription ("desktop" variants).
EMPTY_STATES = {
    ViewFilter.FAVORITES: ("star", "Collect your best content",
                           "You can favorite any document, folder, book, or quick sheet.\n"
                           "They will appear right here."),
    ViewFilter.PDFS: ("document", "Create a reading list",
                      "Drag and drop PDF files from your desktop to add some documents."),
    ViewFilter.EBOOKS: ("ebook", "Upload your library",
                        "Drag and drop EPUB files from your desktop to add some ebooks."),
    ViewFilter.TRASH: ("trashcan", "Clear out the clutter",
                       "Items you delete from My files and folders will appear here. "
                       "You can restore or permanently delete them."),
}

OFFLINE_TIP = "Not available offline"

TYPE_ICONS = {FileType.NOTEBOOK: "notebook", FileType.PDF: "document", FileType.EPUB: "ebook"}


def tool_button(icon_name: str, tooltip: str, text: str = "", size: int = Metrics.icon_medium) -> QToolButton:
    button = QToolButton()
    button.setIcon(icon(icon_name, Colors.gray1, size))
    button.setIconSize(icon_size(size))
    button.setToolTip(tooltip)
    button.setCursor(Qt.CursorShape.PointingHandCursor)
    if text:
        button.setText(text)
        button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        button.setFont(font(Fonts.medium, QFont.Weight.Medium))
    return button


# ------------------------------------------------------------------------ sidebar

class Sidebar(QWidget):
    viewSelected = Signal(object)    # ViewFilter
    actionRequested = Signal(str)    # SidebarItem.action, e.g. "import"

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setFixedWidth(Metrics.sidebar_width)
        self.setMouseTracking(True)
        self.current: ViewFilter | None = ViewFilter.MY_FILES
        self.writable = True
        self._hover = -1
        self._rects: list[QRect] = []
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Expanding)

    def set_current(self, view: ViewFilter | None) -> None:
        self.current = view
        self.update()

    def set_writable(self, writable: bool) -> None:
        self.writable = writable
        self.update()

    def _enabled(self, item) -> bool:
        return item.view is not None or (bool(item.action) and self.writable)

    def _layout(self) -> list[QRect]:
        rects, y, group = [], Metrics.content_margin, SIDEBAR[0].group
        for item in SIDEBAR:
            if item.group != group:
                y += Metrics.content_margin            # divider gap
                group = item.group
            rects.append(QRect(Metrics.base_margin, y,
                               self.width() - 2 * Metrics.base_margin, Metrics.sidebar_item_height))
            y += Metrics.sidebar_item_height
        return rects

    def item_rect(self, view: ViewFilter) -> QRect:
        rects = self._layout()
        return next(rects[i] for i, item in enumerate(SIDEBAR) if item.view == view)

    def sizeHint(self) -> QSize:
        rects = self._layout()
        return QSize(Metrics.sidebar_width, rects[-1].bottom() + Metrics.content_margin)

    def paintEvent(self, event: QPaintEvent) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.fillRect(self.rect(), QColor(Colors.light))
        p.setPen(QPen(QColor(Colors.divider), 1))
        p.drawLine(self.width() - 1, 0, self.width() - 1, self.height())   # Sidebar.borderRight
        self._rects = self._layout()
        group = SIDEBAR[0].group
        for i, (item, rect) in enumerate(zip(SIDEBAR, self._rects)):
            if item.group != group:
                y = rect.top() - Metrics.content_margin // 2
                p.setPen(QPen(QColor(Colors.divider), 1))
                p.drawLine(rect.left(), y, rect.right(), y)
                group = item.group
            enabled = self._enabled(item)
            selected = item.view is not None and item.view == self.current
            if selected or (enabled and i == self._hover):
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(QColor(Colors.selected if selected else Colors.hover))
                p.drawRoundedRect(QRectF(rect), 2, 2)
            color = Colors.text if enabled else Colors.gray5
            img = icon_image(item.icon, color, Metrics.sidebar_icon)
            p.drawImage(QRect(rect.left() + Metrics.base_margin,
                              rect.center().y() - Metrics.sidebar_icon // 2,
                              Metrics.sidebar_icon, Metrics.sidebar_icon), img)
            p.setPen(QColor(color))
            p.setFont(font(Fonts.medium, QFont.Weight.Medium))      # label.md: 14 / Medium
            text_rect = rect.adjusted(Metrics.base_margin * 2 + Metrics.sidebar_icon, 0, 0, 0)
            p.drawText(text_rect, Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, item.label)

    def _index_at(self, pos: QPoint) -> int:
        return next((i for i, r in enumerate(self._layout()) if r.contains(pos)), -1)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        index = self._index_at(event.position().toPoint())
        if index != self._hover:
            self._hover = index
            enabled = index >= 0 and self._enabled(SIDEBAR[index])
            self.setCursor(Qt.CursorShape.PointingHandCursor if enabled else Qt.CursorShape.ArrowCursor)
            tip = OFFLINE_TIP if index >= 0 and SIDEBAR[index].action else "Not available in this client yet"
            self.setToolTip("" if index < 0 or enabled else tip)
            self.update()

    def leaveEvent(self, event) -> None:
        self._hover = -1
        self.update()

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        index = self._index_at(event.position().toPoint())
        if index < 0 or not self._enabled(SIDEBAR[index]):
            return
        if SIDEBAR[index].view is not None:
            self.viewSelected.emit(SIDEBAR[index].view)
        else:
            self.actionRequested.emit(SIDEBAR[index].action)


# ------------------------------------------------------------------------ headers

class ActionRow(QWidget):
    """Back | … | Folder, Import / Empty trash | search | sync  (ActionHeader.qml)."""

    backClicked = Signal()
    searchChanged = Signal(str)
    syncClicked = Signal()
    newFolderClicked = Signal()
    importClicked = Signal()
    emptyTrashClicked = Signal()
    screenShareClicked = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setFixedHeight(Metrics.header_height)
        row = QHBoxLayout(self)
        row.setContentsMargins(Metrics.content_margin, 0, Metrics.content_margin, 0)
        row.setSpacing(Metrics.base_margin)
        self.back = tool_button("chevron_left", "Back", "Back")
        self.back.clicked.connect(self.backClicked)
        row.addWidget(self.back)
        row.addStretch(1)
        # ActionHeader.qml: folder_add "Folder", import "Import", trashcan_delete "Empty trash"
        self.new_folder = tool_button("folder_add", "Create a new folder", "Folder")
        self.new_folder.clicked.connect(self.newFolderClicked)
        self.import_files = tool_button("import", "Import", "Import")
        self.import_files.clicked.connect(self.importClicked)
        self.empty_trash = tool_button("trashcan_delete", "Empty trash", "Empty trash")
        self.empty_trash.clicked.connect(self.emptyTrashClicked)
        for button in (self.new_folder, self.import_files, self.empty_trash):
            row.addWidget(button)
        self.empty_trash.hide()
        # DesktopMenuBar.qml: "Join active Screen Share session" (shown when Screen Share is set up)
        self.screen_share = tool_button("screen_share", "Join active Screen Share session", "Screen Share")
        self.screen_share.clicked.connect(self.screenShareClicked)
        self.screen_share.hide()
        row.addWidget(self.screen_share)
        self.search = QLineEdit()
        self.search.setPlaceholderText("Search")
        self.search.setClearButtonEnabled(True)
        self.search.addAction(icon("lens", Colors.gray2, Metrics.icon_small),
                              QLineEdit.ActionPosition.LeadingPosition)
        self.search.setFixedWidth(240)
        self.search.setFocusPolicy(Qt.FocusPolicy.ClickFocus)    # don't grab focus at startup
        self.search.setFont(font(Fonts.medium))
        self.search.textChanged.connect(self.searchChanged)
        row.addWidget(self.search)
        self.sync = tool_button("arrows_counter_clockwise", "Sync with the cloud")
        self.sync.clicked.connect(self.syncClicked)
        row.addWidget(self.sync)

    def set_syncing(self, syncing: bool) -> None:
        self.sync.setEnabled(not syncing)
        self.sync.setToolTip("Syncing…" if syncing else "Sync with the cloud")

    def set_screen_share(self, available: bool, active: bool = False) -> None:
        self.screen_share.setVisible(available)
        self.screen_share.setEnabled(not active)

    def set_actions(self, *, create: bool, empty_trash: bool, writable: bool, trash_empty: bool) -> None:
        """Folder/Import where items can be created, Empty trash in the Trash."""
        self.new_folder.setVisible(create)
        self.import_files.setVisible(create)
        self.empty_trash.setVisible(empty_trash)
        for button, tip in ((self.new_folder, "Create a new folder"), (self.import_files, "Import"),
                            (self.empty_trash, "Empty trash")):
            button.setEnabled(writable and not (button is self.empty_trash and trash_empty))
            button.setToolTip(tip if writable else OFFLINE_TIP)


class CrumbRow(QWidget):
    """Breadcrumbs, "Sort by" and the grid/list toggle (ContentEntriesHeader.qml)."""

    crumbClicked = Signal(object)     # Location
    sortChanged = Signal(object)      # SortOrder
    modeChanged = Signal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setFixedHeight(Metrics.entries_header_height + Metrics.base_margin)
        row = QHBoxLayout(self)
        row.setContentsMargins(Metrics.content_margin + Metrics.base_margin, 0, Metrics.content_margin, 0)
        row.setSpacing(Metrics.base_margin)
        self.crumbs = QHBoxLayout()
        self.crumbs.setSpacing(Metrics.base_margin)
        row.addLayout(self.crumbs)
        row.addStretch(1)
        # SortOrderButton.qml: label then chevron_small_down (RightToLeft), fontSizeLarge/Medium
        self.sort = tool_button("chevron_small_down", "Sort by", "", Metrics.icon_small)
        self.sort.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.sort.setLayoutDirection(Qt.LayoutDirection.RightToLeft)
        self.sort.setFont(font(Fonts.large, QFont.Weight.Medium))
        self.sort.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.sort.setStyleSheet("QToolButton::menu-indicator { image: none; width: 0; }")
        menu = QMenu(self.sort)
        menu.addSection("Sort by")
        self.sort_actions = {}
        menu.setLayoutDirection(Qt.LayoutDirection.LeftToRight)
        for order in SortOrder:
            action = menu.addAction(order.value)
            action.setCheckable(True)
            action.triggered.connect(lambda _=False, o=order: self.sortChanged.emit(o))
            self.sort_actions[order] = action
        self.sort.setMenu(menu)
        row.addWidget(self.sort)
        self.mode = tool_button("list", "Show as list", "", Metrics.icon_medium)
        self.mode.clicked.connect(self._toggle_mode)
        row.addWidget(self.mode)
        self._mode = GRID
        self._labels: list[QWidget] = []

    def _toggle_mode(self) -> None:
        self.modeChanged.emit(LIST if self._mode == GRID else GRID)

    def set_mode(self, mode: str) -> None:
        self._mode = mode
        other = "list" if mode == GRID else "grid"
        self.mode.setIcon(icon(other, Colors.gray1))
        self.mode.setToolTip("Show as list" if mode == GRID else "Show as grid")

    def set_sort(self, order: SortOrder, in_trash: bool = False) -> None:
        self.sort.setText(order.label(in_trash))
        for o, action in self.sort_actions.items():
            action.setText(o.label(in_trash))
            action.setChecked(o == order)

    def set_crumbs(self, crumbs: list[Crumb]) -> None:
        for w in self._labels:
            self.crumbs.removeWidget(w)
            w.deleteLater()
        self._labels = []
        for i, crumb in enumerate(crumbs):
            if i:
                sep = QLabel()
                sep.setPixmap(QPixmap.fromImage(icon_image("angle_right", Colors.gray1, Metrics.icon_small)))
                self.crumbs.addWidget(sep)
                self._labels.append(sep)
            last = i == len(crumbs) - 1
            button = QToolButton()
            button.setText(crumb.label)
            # PathItem.qml: first crumb uses the large title, the rest the medium one.
            button.setFont(font(Fonts.title if i == 0 else Fonts.large,
                                QFont.Weight.Bold if i == 0 else QFont.Weight.Medium))
            button.setStyleSheet(f"color: {Colors.gray1};")
            button.setEnabled(not last)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.clicked.connect(lambda _=False, loc=crumb.location: self.crumbClicked.emit(loc))
            self.crumbs.addWidget(button)
            self._labels.append(button)
        if self._labels:
            # Disabled styling would grey out the current location; keep it legible.
            self._labels[-1].setStyleSheet(f"color: {Colors.gray1}; background: transparent;")


# --------------------------------------------------------------------- entry view

@dataclass
class _Item:
    rect: QRect          # content coordinates
    entry: Entry
    tile: bool           # folder/tag tile (vs document cell or list row)


class EntryView(QAbstractScrollArea):
    """Custom-painted grid (folder tiles, then 3:4 document cells) or list."""

    activated = Signal(object)            # Entry
    contextRequested = Signal(object, QPoint)   # Entry, global position

    def __init__(self, thumbnails: ThumbnailService | None, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.thumbnails = thumbnails
        self.entries: list[Entry] = []
        self.mode = GRID
        self.empty: tuple[str, str, str] | None = None     # icon, title, description
        self.drop_hint = ""                                # DocumentDropOverlay text while dragging
        self._items: list[_Item] = []
        self._content_height = 0
        self._hover = -1
        self.viewport().setMouseTracking(True)
        self.setFrameShape(QAbstractScrollArea.Shape.NoFrame)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.verticalScrollBar().setSingleStep(40)
        if thumbnails is not None:
            thumbnails.ready.connect(lambda _id: self.viewport().update())
            thumbnails.failed.connect(lambda _id, _why: self.viewport().update())

    # -- data --------------------------------------------------------------------

    def set_drop_hint(self, text: str) -> None:
        self.drop_hint = text
        self.viewport().update()

    def set_entries(self, entries: list[Entry], mode: str, empty=None) -> None:
        self.entries = entries
        self.mode = mode
        self.empty = empty
        self._hover = -1
        self.verticalScrollBar().setValue(0)
        self._relayout()

    def items(self) -> list[_Item]:
        return list(self._items)

    # -- layout ------------------------------------------------------------------

    def _relayout(self) -> None:
        width = self.viewport().width()
        side = Metrics.content_margin * 2 - Metrics.grid_item_margin   # Content.qml leftRightMargin
        avail = max(1, width - 2 * side)
        items: list[_Item] = []
        y = Metrics.base_margin
        if self.mode == GRID:
            tiles = [e for e in self.entries if e.kind != "document"]
            docs = [e for e in self.entries if e.kind == "document"]
            cols = max(1, avail // Metrics.folder_item_width)
            tile_w = avail // cols
            tile_h = Metrics.folder_item_height + Metrics.content_margin
            for i, entry in enumerate(tiles):
                r, c = divmod(i, cols)
                items.append(_Item(QRect(side + c * tile_w, y + r * tile_h, tile_w, tile_h), entry, True))
            if tiles:
                y += ((len(tiles) + cols - 1) // cols) * tile_h + Metrics.base_margin
            cols = max(2, avail // Metrics.grid_item_width)               # minGridColumns 2
            cell_w = avail // cols
            cell_h = round(cell_w * Metrics.page_aspect) + Metrics.grid_bottom_height
            for i, entry in enumerate(docs):
                r, c = divmod(i, cols)
                items.append(_Item(QRect(side + c * cell_w, y + r * cell_h, cell_w, cell_h), entry, False))
            if docs:
                y += ((len(docs) + cols - 1) // cols) * cell_h
        else:
            for i, entry in enumerate(self.entries):
                items.append(_Item(QRect(side, y + i * Metrics.list_item_height, avail,
                                         Metrics.list_item_height), entry, False))
            y += len(self.entries) * Metrics.list_item_height
        self._items = items
        self._content_height = y + Metrics.content_margin
        bar = self.verticalScrollBar()
        bar.setRange(0, max(0, self._content_height - self.viewport().height()))
        bar.setPageStep(self.viewport().height())
        self.viewport().update()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._relayout()

    def scrollContentsBy(self, dx: int, dy: int) -> None:
        self.viewport().update()

    # -- painting ----------------------------------------------------------------

    def paintEvent(self, event: QPaintEvent) -> None:
        p = QPainter(self.viewport())
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        p.fillRect(self.viewport().rect(), QColor(Colors.light))
        if not self._items:
            self._paint_empty(p)
        else:
            self._paint_items(p)
        if self.drop_hint:
            self._paint_drop_hint(p)

    def _paint_drop_hint(self, p: QPainter) -> None:
        p.resetTransform()
        rect = self.viewport().rect().adjusted(Metrics.content_margin, Metrics.content_margin,
                                               -Metrics.content_margin, -Metrics.content_margin)
        veil = QColor(Colors.light)
        veil.setAlpha(225)
        p.fillRect(self.viewport().rect(), veil)
        p.setPen(QPen(QColor(Colors.gray1), 2, Qt.PenStyle.DashLine))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawRoundedRect(QRectF(rect), 4, 4)
        size = Metrics.icon_large
        p.drawImage(QRect(rect.center().x() - size // 2, rect.center().y() - size - 8, size, size),
                    icon_image("import", Colors.gray1, size))
        p.setPen(QColor(Colors.gray1))
        p.setFont(font(Fonts.title, QFont.Weight.Bold))
        p.drawText(QRect(rect.left(), rect.center().y() + 8, rect.width(), 40),
                   Qt.AlignmentFlag.AlignHCenter, self.drop_hint)

    def _paint_items(self, p: QPainter) -> None:
        offset = self.verticalScrollBar().value()
        p.translate(0, -offset)
        visible = QRect(0, offset, self.viewport().width(), self.viewport().height())
        for i, item in enumerate(self._items):
            if not item.rect.intersects(visible):
                continue
            hover = i == self._hover
            if item.tile:
                self._paint_tile(p, item, hover)
            elif self.mode == GRID:
                self._paint_cell(p, item, hover)
            else:
                self._paint_row(p, item, hover)

    def _paint_empty(self, p: QPainter) -> None:
        if not self.empty:
            return
        icon_name, title, description = self.empty
        rect = self.viewport().rect().adjusted(40, 0, -40, 0)
        cy = rect.center().y() - 40
        size = Metrics.icon_large
        p.drawImage(QRect(rect.center().x() - size // 2, cy - size, size, size),
                    icon_image(icon_name, Colors.gray1, size))
        p.setPen(QColor(Colors.gray1))
        p.setFont(font(Fonts.title, QFont.Weight.Bold))
        p.drawText(QRect(rect.left(), cy + Metrics.content_margin, rect.width(), 30),
                   Qt.AlignmentFlag.AlignHCenter, title)
        if description:
            p.setFont(font(Fonts.large))
            p.drawText(QRect(rect.left(), cy + 56, rect.width(), 60),
                       Qt.AlignmentFlag.AlignHCenter | Qt.TextFlag.TextWordWrap, description)

    def _paint_tile(self, p: QPainter, item: _Item, hover: bool) -> None:
        # ContentEntriesGridItem.qml folderComponent: gray4 fill, gray3 on hover, radius 2
        rect = item.rect.adjusted(Metrics.grid_item_margin, Metrics.grid_item_margin,
                                  -Metrics.grid_item_margin, -Metrics.grid_item_margin)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(Colors.gray3 if hover else Colors.gray4))
        p.drawRoundedRect(QRectF(rect), Metrics.folder_radius, Metrics.folder_radius)
        entry = item.entry
        if entry.kind == "tag":
            icon_name = "tag"
        else:
            icon_name = "folder_inverted" if entry.count else "folder"   # entry.hasChildren
        size = Metrics.icon_medium
        p.drawImage(QRect(rect.left() + Metrics.folder_inner_margin, rect.center().y() - size // 2,
                          size, size), icon_image(icon_name, Colors.gray1, size))
        right_inset = Metrics.folder_inner_margin
        if entry.pinned:
            s = Metrics.icon_small
            p.drawImage(QRect(rect.right() - Metrics.folder_inner_margin - s, rect.center().y() - s // 2, s, s),
                        icon_image("star_inverted", Colors.gray1, s))
            right_inset += s + Metrics.folder_inner_margin
        text_rect = rect.adjusted(Metrics.folder_inner_margin * 2 + size, 0, -right_inset, 0)
        p.setPen(QColor(Colors.gray1))
        f = font(Fonts.medium, QFont.Weight.Medium)
        p.setFont(f)
        p.drawText(text_rect, Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
                   QFontMetrics(f).elidedText(entry.title, Qt.TextElideMode.ElideMiddle, text_rect.width()))

    @staticmethod
    def _thumbnail_rect(slot: QRect, image: QImage | None) -> QRect:
        """Fit the thumbnail into the 3:4 slot, centred, keeping its own aspect
        (PDF pages are not 3:4)."""
        if image is None or image.isNull():
            return slot
        scale = min(slot.width() / image.width(), slot.height() / image.height())
        w, h = round(image.width() * scale), round(image.height() * scale)
        return QRect(slot.center().x() - w // 2 + 1, slot.top() + (slot.height() - h) // 2, w, h)

    def _paint_thumbnail(self, p: QPainter, slot: QRect, entry: Entry) -> None:
        doc = entry.node
        image = self.thumbnails.thumbnail(doc) if (self.thumbnails and isinstance(doc, Document)) else None
        target = self._thumbnail_rect(slot, image)
        if image is not None:
            p.drawImage(target, image)
        else:
            p.fillRect(target, QColor(Colors.gray4))
            size = Metrics.icon_large
            name = TYPE_ICONS.get(doc.file_type, "notebook") if isinstance(doc, Document) else "notebook"
            p.drawImage(QRect(target.center().x() - size // 2, target.center().y() - size // 2, size, size),
                        icon_image(name, Colors.gray2, size))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.setPen(QPen(QColor(Colors.gray3), Metrics.thumbnail_border))     # cover border 2 px
        p.drawRect(QRectF(target).adjusted(-1, -1, 1, 1))

    def _paint_cell(self, p: QPainter, item: _Item, hover: bool) -> None:
        rect = item.rect
        m = Metrics.grid_item_margin
        slot = QRect(rect.left() + m, rect.top() + m, rect.width() - 2 * m,
                     round(rect.width() * Metrics.page_aspect) - 2 * m)
        if hover:
            p.fillRect(rect.adjusted(2, 2, -2, -2), QColor(Colors.hover))
        self._paint_thumbnail(p, slot, item.entry)
        bottom = QRect(rect.left() + m, slot.bottom() + m, rect.width() - 2 * m, Metrics.grid_bottom_height)
        self._paint_labels(p, bottom, item.entry)

    def _paint_labels(self, p: QPainter, rect: QRect, entry: Entry) -> None:
        right = 0
        if entry.pinned:
            s = Metrics.icon_small
            p.drawImage(QRect(rect.right() - s, rect.top() + 2, s, s),
                        icon_image("star_inverted", Colors.gray1, s))
            right = s + Metrics.base_margin
        title_font = font(Fonts.large, QFont.Weight.Medium)          # fontSizeLarge, Medium
        p.setFont(title_font)
        p.setPen(QColor(Colors.text))
        title_rect = QRect(rect.left(), rect.top(), rect.width() - right, Fonts.large + 6)
        p.drawText(title_rect, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                   QFontMetrics(title_font).elidedText(entry.title, Qt.TextElideMode.ElideMiddle, title_rect.width()))
        sub_font = font(Fonts.medium)                                # fontSizeMedium
        p.setFont(sub_font)
        p.setPen(QColor(Colors.text_secondary))
        sub_rect = QRect(rect.left(), title_rect.bottom() + 2, rect.width(), Fonts.medium + 6)
        p.drawText(sub_rect, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                   QFontMetrics(sub_font).elidedText(entry.subtext, Qt.TextElideMode.ElideRight, sub_rect.width()))

    def _paint_row(self, p: QPainter, item: _Item, hover: bool) -> None:
        # ContentEntriesListItem.qml: type icon, 3:4 thumbnail, title + subtext, hairline
        rect = item.rect
        if hover:
            p.fillRect(rect, QColor(Colors.hover))
        p.setPen(QPen(QColor(Colors.gray3), 1))
        p.drawLine(rect.left(), rect.bottom(), rect.right(), rect.bottom())
        entry = item.entry
        size = Metrics.icon_medium
        if entry.kind == "document":
            name = TYPE_ICONS.get(entry.node.file_type, "notebook")
        else:
            name = "tag" if entry.kind == "tag" else "folder"
        x = rect.left() + Metrics.base_margin
        p.drawImage(QRect(x, rect.center().y() - size // 2, size, size), icon_image(name, Colors.gray1, size))
        x += size + Metrics.content_margin
        if entry.kind == "document":
            h = rect.height() - 2 * Metrics.content_margin
            slot = QRect(x, rect.top() + Metrics.content_margin, round(h / Metrics.page_aspect), h)
            self._paint_thumbnail(p, slot, entry)
            x = slot.right() + Metrics.content_margin
        labels = QRect(x, rect.center().y() - 20, rect.right() - x - Metrics.base_margin * 3, 44)
        self._paint_labels(p, labels, entry)

    # -- interaction -------------------------------------------------------------

    def index_at(self, pos: QPoint) -> int:
        point = QPoint(pos.x(), pos.y() + self.verticalScrollBar().value())
        return next((i for i, item in enumerate(self._items) if item.rect.contains(point)), -1)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        index = self.index_at(event.position().toPoint())
        if index != self._hover:
            self._hover = index
            self.viewport().setCursor(Qt.CursorShape.PointingHandCursor if index >= 0
                                      else Qt.CursorShape.ArrowCursor)
            self.viewport().update()

    def leaveEvent(self, event) -> None:
        self._hover = -1
        self.viewport().update()

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if event.button() != Qt.MouseButton.LeftButton:
            return
        index = self.index_at(event.position().toPoint())
        if index >= 0:
            self.activated.emit(self._items[index].entry)

    def contextMenuEvent(self, event: QContextMenuEvent) -> None:
        index = self.index_at(event.pos())          # forwarded by the viewport, in its coordinates
        if index >= 0:
            self.contextRequested.emit(self._items[index].entry, event.globalPos())


# ------------------------------------------------------------------------ dialogs

class FolderPicker(QDialog):
    """Pick a destination folder: My files and its folders, never the trash or a
    folder inside one that is being moved."""

    def __init__(self, tree: DocumentTree, moving: list[Node], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Move")
        self.resize(360, 420)
        self.folders = QTreeWidget()
        self.folders.setHeaderHidden(True)
        root = QTreeWidgetItem(["My files"])
        root.setData(0, Qt.ItemDataRole.UserRole, ROOT_ID)
        root.setIcon(0, icon("my_files", Colors.gray1, Metrics.icon_small))
        self.folders.addTopLevelItem(root)
        excluded = {n.id for n in moving if n.is_folder}
        seen: set[str] = set()

        def add(item: QTreeWidgetItem, folder_id: str) -> None:
            for child in tree.children(folder_id):
                if child.is_folder and child.id not in excluded and child.id not in seen:
                    seen.add(child.id)
                    sub = QTreeWidgetItem(item, [child.name])
                    sub.setData(0, Qt.ItemDataRole.UserRole, child.id)
                    sub.setIcon(0, icon("folder", Colors.gray1, Metrics.icon_small))
                    add(sub, child.id)
        add(root, ROOT_ID)
        root.setExpanded(True)
        self.folders.setCurrentItem(root)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel)
        buttons.addButton("Move", QDialogButtonBox.ButtonRole.AcceptRole)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout = QVBoxLayout(self)
        layout.addWidget(self.folders)
        layout.addWidget(buttons)

    def folder_ids(self) -> list[str]:
        ids, stack = [], [self.folders.topLevelItem(0)]
        while stack:
            item = stack.pop()
            ids.append(item.data(0, Qt.ItemDataRole.UserRole))
            stack.extend(item.child(i) for i in range(item.childCount()))
        return ids

    def choose(self) -> str | None:
        if self.exec() != QDialog.DialogCode.Accepted or self.folders.currentItem() is None:
            return None
        return self.folders.currentItem().data(0, Qt.ItemDataRole.UserRole)


def _items_were(n: int, what: str) -> str:
    """LibraryHelpers.qml: "%1 items were moved to Trash" / "... restored to My files"."""
    return f"1 item was {what}" if n == 1 else f"{n} items were {what}"


# ---------------------------------------------------------------------- navigator

class Navigator(QWidget):
    """Sidebar + headers + entry view, driven by a LibraryQuery."""

    openDocument = Signal(object)     # Document
    syncRequested = Signal()
    changeRequested = Signal(str, object, str)    # what, change(sync, progress), done message

    def __init__(self, thumbnails: ThumbnailService | None, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.query: LibraryQuery | None = None
        self.location = Location()
        self.loading = False
        self.sort = SortOrder.LAST_MODIFIED
        self.mode = GRID
        self.history: list[Location] = []
        self.writable = True
        self.setAcceptDrops(True)

        self.sidebar = Sidebar()
        self.actions = ActionRow()
        self.crumb_row = CrumbRow()
        self.view = EntryView(thumbnails)
        right = QVBoxLayout()
        right.setContentsMargins(0, 0, 0, 0)
        right.setSpacing(0)
        right.addWidget(self.actions)
        right.addWidget(self.crumb_row)
        right.addWidget(self.view, 1)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(self.sidebar)
        layout.addLayout(right, 1)

        self.sidebar.viewSelected.connect(lambda v: self.navigate(Location(v), clear_search=True))
        self.actions.backClicked.connect(self.back)
        self.actions.searchChanged.connect(self._search)
        self.actions.syncClicked.connect(self.syncRequested)
        self.crumb_row.crumbClicked.connect(lambda loc: self.navigate(loc, clear_search=True))
        self.crumb_row.sortChanged.connect(self.set_sort)
        self.crumb_row.modeChanged.connect(self.set_mode)
        self.view.activated.connect(self._activate)
        self.sidebar.actionRequested.connect(self._sidebar_action)
        self.actions.newFolderClicked.connect(self.new_folder)
        self.actions.importClicked.connect(lambda: self.import_files())
        self.actions.emptyTrashClicked.connect(self.empty_trash)
        self.view.contextRequested.connect(self.show_context_menu)
        self.crumb_row.set_sort(self.sort)
        self.crumb_row.set_mode(self.mode)

    def set_tree(self, tree: DocumentTree, now=None) -> None:
        self.query = LibraryQuery(tree, now)
        if not self.query.is_valid(self.location):
            self.location = Location(self.location.view)
        self.history = [loc for loc in self.history if self.query.is_valid(loc)]
        self.reload(keep_scroll=True)

    def reload(self, keep_scroll: bool = False) -> None:
        if self.query is None:
            return
        scroll = self.view.verticalScrollBar().value()
        entries = self.query.entries(self.location, self.sort)
        empty = None
        if not entries:
            if self.loading and not len(self.query.tree):
                empty = ("arrows_counter_clockwise", "Loading your library…", "")
            elif self.location.search.strip():
                empty = ("lens", "No results", "")
            elif self.location.view in EMPTY_STATES and self.location.folder_id in (ROOT_ID, TRASH_ID):
                empty = EMPTY_STATES[self.location.view]
            else:
                empty = ("folder", "This folder is empty", "")
        self.view.set_entries(entries, self.mode, empty)
        if keep_scroll:
            self.view.verticalScrollBar().setValue(scroll)
        self.crumb_row.set_crumbs(self.query.breadcrumbs(self.location))
        self.crumb_row.set_sort(self.sort, in_trash=self.location.view == ViewFilter.TRASH)
        self.sidebar.set_current(None if self.location.search.strip() else self.location.view)
        self.actions.back.setEnabled(bool(self.history))
        self.actions.set_actions(create=self.location.view == ViewFilter.MY_FILES and not self.location.search.strip(),
                                 empty_trash=self.location.view == ViewFilter.TRASH,
                                 writable=self.writable, trash_empty=not self.trash_ids())

    def set_writable(self, writable: bool) -> None:
        self.writable = writable
        self.sidebar.set_writable(writable)
        self.reload(keep_scroll=True)

    def navigate(self, loc: Location, clear_search: bool = False) -> None:
        if clear_search and self.actions.search.text():
            self.actions.search.blockSignals(True)
            self.actions.search.clear()
            self.actions.search.blockSignals(False)
        if loc != self.location:
            self.history.append(self.location)
        self.location = loc
        self.reload()

    def back(self) -> None:
        if self.history:
            self.location = self.history.pop()
            self.actions.search.blockSignals(True)
            self.actions.search.setText(self.location.search)
            self.actions.search.blockSignals(False)
            self.reload()

    def _search(self, text: str) -> None:
        base = Location(self.location.view, self.location.folder_id, self.location.tag)
        # Typing refines the current search instead of stacking history entries.
        if not self.location.search.strip() and text.strip():
            self.history.append(self.location)
        self.location = Location(base.view, base.folder_id, base.tag, search=text)
        self.reload()

    def set_sort(self, order: SortOrder) -> None:
        self.sort = order
        self.reload()

    def set_loading(self, loading: bool) -> None:
        self.loading = loading
        self.reload(keep_scroll=True)

    def set_mode(self, mode: str) -> None:
        self.mode = mode
        self.crumb_row.set_mode(mode)
        self.reload(keep_scroll=True)

    def _activate(self, entry: Entry) -> None:
        if entry.kind == "document":
            self.openDocument.emit(entry.node)
        else:
            self.navigate(self.query.open_location(self.location, entry), clear_search=True)

    # -- changes -------------------------------------------------------------------

    def current_folder(self) -> str:
        """Where new folders and imports go: the open folder in My files, else My files."""
        return self.location.folder_id if self.location.view == ViewFilter.MY_FILES else ROOT_ID

    def trash_ids(self) -> list[str]:
        return [n.id for n in self.query.tree.children(TRASH_ID)] if self.query else []

    def _request(self, what: str, change: Callable, done: str) -> None:
        if self.writable:
            self.changeRequested.emit(what, change, done)

    # Dialogs, separate so tests can answer them.
    def ask_text(self, title: str, label: str, text: str, ok: str) -> str | None:
        dialog = QInputDialog(self)
        dialog.setWindowTitle(title)
        dialog.setLabelText(label)
        dialog.setTextValue(text)
        dialog.setOkButtonText(ok)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return None
        return dialog.textValue().strip() or None

    def ask_folder(self, moving: list[Node]) -> str | None:
        return FolderPicker(self.query.tree, moving, self).choose()

    def confirm(self, title: str, text: str, ok: str) -> bool:
        box = QMessageBox(QMessageBox.Icon.Warning, title, text, parent=self)
        cancel = box.addButton("Cancel", QMessageBox.ButtonRole.RejectRole)
        accept = box.addButton(ok, QMessageBox.ButtonRole.DestructiveRole)
        box.setDefaultButton(cancel)
        box.exec()
        return box.clickedButton() is accept

    def ask_import_files(self) -> list[str]:
        paths, _ = QFileDialog.getOpenFileNames(self, "Select files to import", "",
                                                "Documents (*.pdf *.epub);;PDF (*.pdf);;EPUB (*.epub)")
        return paths

    def _sidebar_action(self, action: str) -> None:
        if action == "import":
            self.import_files()

    def new_folder(self) -> None:
        if not self.writable or self.query is None:
            return
        parent = self.current_folder()
        taken = {n.name for n in self.query.tree.children(parent)}
        name = self.ask_text("New folder", "Name", unique_name("Folder", taken), "Create")
        if name:
            self._request(f"Creating {name}", lambda sync, _: sync.create_folder(name, parent),
                          f"Created folder {name}")

    def import_files(self, paths: list[str] | None = None) -> None:
        if not self.writable:
            return
        paths = self.ask_import_files() if paths is None else paths
        parent = self.current_folder()
        for path in paths:
            name = Path(path).name
            self._request(f"Uploading {name}",
                          lambda sync, progress, path=path: sync.import_file(path, parent, progress=progress),
                          f"Imported {name}")

    def entry_actions(self, entry: Entry) -> list[tuple[str, Callable[[], None], bool | None] | None]:
        """The item's context menu (ContextMenu.qml): (label, handler, checked) rows,
        None for a separator. Tags have none."""
        node = entry.node
        if node is None or self.query is None:
            return []
        if self.query.tree.is_trashed(node.id):
            return [("Restore", lambda: self.restore([node]), None), None,
                    ("Delete permanently…", lambda: self.delete([node]), None)]
        return [("Favorite", lambda: self.toggle_favorite(node), node.pinned),
                ("Rename…", lambda: self.rename(node), None),
                ("Move…", lambda: self.move([node]), None), None,
                ("Move to trash", lambda: self.trash([node]), None)]

    def context_menu(self, entry: Entry) -> QMenu | None:
        rows = self.entry_actions(entry)
        if not rows:
            return None
        menu = QMenu(self)
        for row in rows:
            if row is None:
                menu.addSeparator()
                continue
            label, handler, checked = row
            action = menu.addAction(label)
            action.setEnabled(self.writable)
            if checked is not None:
                action.setCheckable(True)
                action.setChecked(checked)
            action.triggered.connect(handler)
        return menu

    def show_context_menu(self, entry: Entry, pos: QPoint) -> None:
        menu = self.context_menu(entry)
        if menu is not None:
            menu.exec(pos)
            menu.deleteLater()

    def toggle_favorite(self, node: Node) -> None:
        pin = not node.pinned
        self._request("Favorite" if pin else "Unfavorite", lambda sync, _: sync.set_pinned([node.id], pin),
                      f"Added {node.name} to Favorites" if pin else f"Removed {node.name} from Favorites")

    def rename(self, node: Node) -> None:
        name = self.ask_text("Rename", "Name", node.name, "Save")
        if name and name != node.name:
            self._request(f"Renaming {node.name}", lambda sync, _: sync.rename(node.id, name), f"Renamed to {name}")

    def move(self, nodes: list[Node]) -> None:
        target = self.ask_folder(nodes)
        if target is None:
            return
        label = "My files" if target == ROOT_ID else self.query.tree.get(target).name
        ids = [n.id for n in nodes]
        self._request(f"Moving to {label}", lambda sync, _: sync.move(ids, target), f"Moved to {label}")

    def trash(self, nodes: list[Node]) -> None:
        ids = [n.id for n in nodes]
        self._request("Moving to trash", lambda sync, _: sync.trash(ids), _items_were(len(ids), "moved to Trash"))

    def restore(self, nodes: list[Node]) -> None:
        ids = [n.id for n in nodes]
        self._request("Restoring", lambda sync, _: sync.restore(ids), _items_were(len(ids), "restored to My files"))

    def delete(self, nodes: list[Node]) -> None:
        many = len(nodes) > 1
        if not self.confirm("Delete items permanently?" if many else "Delete item permanently?",
                            f"The item{'s' if many else ''} will be deleted permanently across all your "
                            "synced devices.", "Delete"):
            return
        ids = [n.id for n in nodes]
        self._request("Deleting", lambda sync, _: sync.delete(ids), _items_were(len(ids), "deleted"))

    def empty_trash(self) -> None:
        ids = self.trash_ids()          # what the user sees in the Trash, nothing more
        if not ids or not self.confirm("Empty trash?", "The items will be deleted permanently across "
                                       "all your synced devices.", "Empty trash"):
            return
        self._request("Emptying trash", lambda sync, _: sync.delete(ids), "Trash emptied")

    # -- drag and drop (DocumentDropOverlay.qml) --------------------------------------

    @staticmethod
    def _importable(event) -> list[str]:
        mime = event.mimeData()
        if not mime.hasUrls():
            return []
        return [url.toLocalFile() for url in mime.urls()
                if url.isLocalFile() and Path(url.toLocalFile()).suffix.lower() in IMPORT_TYPES]

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:
        if not (self.writable and self.query is not None and self._importable(event)):
            event.ignore()
            return
        event.acceptProposedAction()
        folder = self.query.tree.get(self.current_folder())
        self.view.set_drop_hint(f"Drop file to upload to {folder.name}" if folder is not None
                                else "Drop file to upload to your reMarkable")

    def dragMoveEvent(self, event) -> None:
        if self.view.drop_hint:
            event.acceptProposedAction()

    def dragLeaveEvent(self, event) -> None:
        self.view.set_drop_hint("")

    def dropEvent(self, event: QDropEvent) -> None:
        self.view.set_drop_hint("")
        files = self._importable(event)
        if files and self.writable:
            event.acceptProposedAction()
            self.import_files(files)
