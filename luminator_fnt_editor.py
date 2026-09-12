import math
import sys
import os
import json
import re
import subprocess
import tempfile
from pathlib import Path
from PyQt6.QtCore import Qt, QSize, QPoint, QTimer, QSettings, QEvent, QByteArray, pyqtSignal
from PyQt6.QtGui import QAction, QFont, QPainter, QColor, QPen, QImage, QIcon, QPixmap
from PyQt6.QtSvg import QSvgRenderer
from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QFileDialog, QMessageBox,
    QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QSpinBox, QToolBar, QStatusBar, QFrame, QSizePolicy, QScrollArea,
    QGroupBox, QGridLayout, QTableWidget, QTableWidgetItem, QSlider,
    QAbstractItemView, QHeaderView, QInputDialog, QDialog, QDialogButtonBox,
    QLineEdit, QFormLayout, QStyledItemDelegate, QStyle, QMenu, QToolButton
    , QComboBox
)

_UNDO_SVG = """
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none"
     stroke="#F8FAFC" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
  <polyline points="9 14 4 9 9 4"></polyline>
  <path d="M20 20v-7a4 4 0 0 0-4-4H4"></path>
</svg>
"""

_REDO_SVG = """
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none"
     stroke="#F8FAFC" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
  <polyline points="15 14 20 9 15 4"></polyline>
  <path d="M4 20v-7a4 4 0 0 1 4-4h12"></path>
</svg>
"""


def _svg_icon(svg_text, size=18):
    """Rasterize an inline SVG string into a QIcon at the given pixel size."""
    renderer = QSvgRenderer(QByteArray(svg_text.encode("utf-8")))
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    renderer.render(painter)
    painter.end()
    return QIcon(pixmap)

class LuminatorFont:
    # In the supplied Luminator files the offset table begins at byte 28.
    # Offsets are 16-bit BIG-ENDIAN values measured relative to byte 28.
    OFFSET_BASE = 28

    def __init__(self, path):
        self.path = Path(path)
        self.raw = self.path.read_bytes()
        if len(self.raw) < 40:
            raise ValueError("File is too small to be a Luminator FNT.")

        self.height = self.raw[22]
        # Header byte 23 is the inter-character spacing used by IPS.
        # In the supplied fonts this is typically 1 or 2 pixels.
        self.spacing = self.raw[23]
        self.first = self.raw[24]
        self.last = self.raw[25]
        if not (1 <= self.height <= 32 and self.first <= self.last):
            raise ValueError("Header does not look like the supported Luminator FNT format.")

        self.bytes_per_col = (self.height + 7) // 8
        self.count = self.last - self.first + 1
        b = self.OFFSET_BASE

        table_bytes = self.count * 2
        if b + table_bytes > len(self.raw):
            raise ValueError("Font offset table runs past end of file.")

        self.offsets = [
            int.from_bytes(self.raw[b+2*i:b+2*i+2], "big")
            for i in range(self.count)
        ]

        if not self.offsets:
            raise ValueError("No glyph offsets found.")

        # The normal Luminator layout contains N glyph-start offsets followed
        # by one final end/sentinel offset. Older revisions of this editor
        # treated those two bytes as a generic table gap. Detect the sentinel
        # explicitly so newly-created fonts can be serialized correctly.
        self.has_end_offset = False
        self.end_offset = None
        sentinel_pos = b + table_bytes
        if sentinel_pos + 2 <= len(self.raw):
            candidate_end = int.from_bytes(
                self.raw[sentinel_pos:sentinel_pos+2], "big"
            )
            expected_min = table_bytes + 2
            if (
                self.offsets[0] >= expected_min
                and candidate_end >= self.offsets[-1]
                and b + candidate_end <= len(self.raw)
            ):
                self.has_end_offset = True
                self.end_offset = candidate_end

        # Sanity checks from the observed format.
        if any(self.offsets[i] > self.offsets[i+1] for i in range(len(self.offsets)-1)):
            raise ValueError("Glyph offsets are not monotonically increasing.")

        minimum_table = table_bytes + (2 if self.has_end_offset else 0)
        if self.offsets[0] < minimum_table:
            raise ValueError("First glyph offset overlaps the offset table.")

        self.prefix = self.raw[:b]
        table_end = b + table_bytes + (2 if self.has_end_offset else 0)
        data_start = b + self.offsets[0]
        if data_start > len(self.raw):
            raise ValueError("First glyph offset points outside file.")

        self.table_gap = self.raw[table_end:data_start]

        self.glyphs = []
        for i, off in enumerate(self.offsets):
            start = b + off
            if i + 1 < self.count:
                end = b + self.offsets[i+1]
            elif self.has_end_offset:
                end = b + self.end_offset
            else:
                end = len(self.raw)

            if start > end or end > len(self.raw):
                raise ValueError(f"Invalid byte range for glyph {self.first+i:#x}.")

            blob = bytearray(self.raw[start:end])

            # Some valid Luminator fonts omit trailing zero byte(s) from the
            # final column of a glyph. L16tnvw.fnt does this for ".":
            # a 16-pixel-high, 3-column glyph is stored as 5 bytes instead of
            # the canonical 6 because the final blank byte is omitted.
            #
            # Treat the missing bytes as implicit zeros. Internally, glyphs
            # are normalized to complete columns so editing/export logic can
            # continue using the normal bytes-per-column representation.
            remainder = len(blob) % self.bytes_per_col
            if remainder:
                missing = self.bytes_per_col - remainder
                blob.extend(b"\x00" * missing)

            self.glyphs.append(blob)

    @classmethod
    def create_blank(cls, name, height, spacing, first, last, glyph_width):
        """Create a new blank Luminator FNT model entirely in memory."""
        height = int(height)
        spacing = int(spacing)
        first = int(first)
        last = int(last)
        glyph_width = int(glyph_width)

        if not 1 <= height <= 32:
            raise ValueError("Font height must be between 1 and 32 pixels.")
        if not 0 <= spacing <= 255:
            raise ValueError("Letter spacing must be between 0 and 255 pixels.")
        if not (0 <= first <= last <= 255):
            raise ValueError("Character range must be between 0x00 and 0xFF.")
        if glyph_width < 1:
            raise ValueError("Initial glyph width must be at least 1 pixel.")

        obj = cls.__new__(cls)
        obj.path = None
        obj.raw = b""
        obj.height = height
        obj.spacing = spacing
        obj.first = first
        obj.last = last
        obj.bytes_per_col = (height + 7) // 8
        obj.count = last - first + 1

        # New files use the normal observed Luminator layout: N start offsets
        # followed by an N+1 end/sentinel offset.
        obj.has_end_offset = True
        obj.end_offset = None
        obj.offsets = []
        obj.table_gap = b""

        # The supported files have a 28-byte prefix.  Bytes 0..19 are used as
        # a short ASCII descriptor; the structural fields are at 22..25.
        prefix = bytearray(28)
        descriptor = (name or "NEW FONT").upper().encode("ascii", "replace")[:20]
        prefix[:len(descriptor)] = descriptor
        prefix[22] = height & 0xFF
        prefix[23] = spacing & 0xFF
        prefix[24] = first & 0xFF
        prefix[25] = last & 0xFF
        prefix[26] = 0
        prefix[27] = 0
        obj.prefix = bytes(prefix)

        blank = bytearray(glyph_width * obj.bytes_per_col)
        obj.glyphs = [bytearray(blank) for _ in range(obj.count)]
        return obj

    @classmethod
    def from_bbm(cls, path, custom_order=False):
        """Import the known Axion BBM bitmap-font layouts as an editable FNT model."""
        path = Path(path)
        raw = path.read_bytes()

        # Axion BBM uses fixed-size glyph slots. The slot width includes one
        # width byte followed by padded column data; the 10/11-pixel fonts
        # store two bytes per column.
        layouts = {
            666: (18, 6, 108, 5, 1),
            888: (24, 8, 108, 7, 1),
            1221: (22, 11, 109, 10, 2),
            1665: (15, 15, 110, 14, 2),
            1887: (17, 17, 110, 16, 2),
            2109: (19, 19, 110, 18, 2),
            2775: (25, 25, 110, 24, 2),
        }
        layout = layouts.get(len(raw))
        if layout is None and len(raw) % 111 == 0:
            # Larger Axion fonts use a header the same size as each of their
            # 110 glyph slots. Their encoded widths reveal the column packing.
            slot_size = len(raw) // 111
            encoded_widths = [
                raw[slot_size + index * slot_size]
                for index in range(110)
            ]
            bytes_per_column = 0
            for width in encoded_widths:
                if width:
                    bytes_per_column = (
                        width if bytes_per_column == 0
                        else math.gcd(bytes_per_column, width)
                    )
            if bytes_per_column in (1, 2, 3, 4) and all(
                width <= slot_size - 1 for width in encoded_widths
            ):
                layout = (slot_size, slot_size, 110, slot_size - 1, bytes_per_column)

        if layout is None:
            raise ValueError(
                f"Unsupported BBM size ({len(raw)} bytes). "
                "This importer supports fixed-slot Axion BBM layouts only."
            )

        header_size, slot_size, count, max_width, bytes_per_column = layout

        if header_size + slot_size * count != len(raw):
            raise ValueError("BBM payload does not match its expected glyph-slot layout.")

        match = re.search(r"x(\d+)", path.stem, re.IGNORECASE)
        if match:
            height = int(match.group(1))
        elif path.stem.upper() == "F-DIGITAL":
            height = 8
        elif match := re.search(r"-(\d+)(?:\s|$)", path.stem):
            height = int(match.group(1))
        elif len(raw) % 111 == 0:
            height = bytes_per_column * 8
        else:
            raise ValueError(
                "Could not determine the BBM glyph height from its filename "
                "(expected a name such as F-DF5x7N.BBM)."
            )
        if path.stem.upper().startswith("OUTLINE"):
            height = min(height + 2, bytes_per_column * 8)
        if not 1 <= height <= bytes_per_column * 8:
            raise ValueError("BBM height and byte layout do not agree.")

        # The 110-slot large layouts begin with "!". The original 109-slot
        # 10/11-pixel family begins with a quote, while the short 108-slot
        # layouts begin with "#".
        first = 0x21 if count == 110 else 0x22 if height >= 10 else 0x23
        last = first + count - 1
        spacing = 2 if height >= 10 else 1
        obj = cls.create_blank(path.stem, height, spacing, first, last, 1)
        obj.path = path
        obj.raw = raw
        obj.bytes_per_col = bytes_per_column
        obj.glyphs = []

        bit_offset = bytes_per_column * 8 - height

        for index in range(count):
            start = header_size + index * slot_size
            slot = raw[start:start + slot_size]
            encoded_width = slot[0]
            if encoded_width > max_width:
                raise ValueError(
                    f"BBM glyph {index} declares width {encoded_width}, "
                    f"outside the supported range 0-{max_width}."
                )
            if bytes_per_column == 2 and encoded_width % 2:
                raise ValueError(
                    f"BBM glyph {index} has an odd two-byte-column width "
                    f"({encoded_width})."
                )
            width = max(1, (encoded_width + bytes_per_column - 1) // bytes_per_column)
            column_bytes = slot[1:1 + encoded_width]
            glyph = bytearray(width * bytes_per_column)

            # BBM stores each column as a packed integer. Short fonts use one
            # byte per column; 10/11-pixel fonts use a big-endian 16-bit value.
            # The useful bitmap starts at a format-specific bit offset.
            for x in range(width):
                for y in range(height):
                    column_start = x * bytes_per_column
                    column_data = column_bytes[column_start:column_start + bytes_per_column]
                    column_value = int.from_bytes(column_data, "big")
                    if column_value & (1 << (bit_offset + y)):
                        target_y = height - 1 - y
                        target_chunk = bytes_per_column - 1 - (target_y // 8)
                        glyph[x * bytes_per_column + target_chunk] |= 1 << (target_y % 8)

            obj.glyphs.append(glyph)

        obj.count = count
        obj.display_labels = (
            bbm_display_labels(first, count)
            if custom_order else bbm_cp437_display_labels(first, count)
        )

        # BBM files begin after ASCII space. Add the missing leading slots to
        # the editable model so every exporter handles space as a real glyph.
        if first > 0x20:
            space = bytearray(3 * obj.bytes_per_col)
            blank = bytearray(obj.bytes_per_col)
            obj.glyphs = [space] + [
                bytearray(blank) for _ in range(first - 0x21)
            ] + obj.glyphs
            obj.first = 0x20
            obj.count = len(obj.glyphs)
        obj.has_end_offset = True
        obj.end_offset = None
        obj.offsets = []
        obj.table_gap = b""
        prefix = bytearray(28)
        descriptor = path.stem.upper().encode("ascii", "replace")[:20]
        prefix[:len(descriptor)] = descriptor
        obj.prefix = bytes(prefix)
        return obj

    @classmethod
    def from_signmatrix(cls, json_path):
        json_path = Path(json_path)

        try:
            metadata = json.loads(json_path.read_text(encoding="utf-8"))
        except Exception as e:
            raise ValueError(f"Could not read SignMatrix JSON: {e}") from e

        if not isinstance(metadata, dict):
            raise ValueError("SignMatrix JSON must contain an object at the top level.")

        try:
            height = int(metadata["height"])
        except Exception as e:
            raise ValueError("SignMatrix JSON is missing a valid 'height'.") from e

        if not 1 <= height <= 32:
            raise ValueError(
                f"Font height {height} is outside the supported Luminator range (1-32)."
            )

        try:
            spacing = int(metadata.get("advance", 1))
        except Exception as e:
            raise ValueError("SignMatrix 'advance' must be an integer.") from e
        spacing = max(0, min(255, spacing))

        img_name = metadata.get("img")
        if not isinstance(img_name, str) or not img_name.strip():
            raise ValueError("SignMatrix JSON is missing the companion 'img' filename.")

        image_path = json_path.parent / img_name
        if not image_path.exists():
            raise ValueError(
                f"Companion SignMatrix image was not found:\n{image_path}"
            )

        image = QImage(str(image_path))
        if image.isNull():
            raise ValueError(f"Could not open SignMatrix image:\n{image_path}")

        glyph_entries = metadata.get("glyphs")
        if not isinstance(glyph_entries, list) or not glyph_entries:
            raise ValueError("SignMatrix JSON does not contain any glyph entries.")

        parsed = {}
        parsed_order = []
        for n, entry in enumerate(glyph_entries, start=1):
            if not isinstance(entry, dict):
                raise ValueError(f"Glyph entry {n} is not an object.")

            ch = entry.get("char")
            if not isinstance(ch, str) or len(ch) != 1:
                raise ValueError(
                    f"Glyph entry {n} must contain exactly one character in 'char'."
                )

            try:
                x = int(entry["x"])
                # Some SignMatrix JSON files omit y when the glyph is on the
                # first sprite-sheet row. Treat an omitted y as 0.
                y = int(entry.get("y", 0))
                width = int(entry["w"])
            except Exception as e:
                raise ValueError(
                    f"Glyph {ch!r} is missing valid x/w coordinates, "
                    "or contains an invalid y coordinate."
                ) from e

            if width < 1:
                raise ValueError(f"Glyph {ch!r} has invalid width {width}.")
            if x < 0 or y < 0 or x + width > image.width() or y + height > image.height():
                raise ValueError(
                    f"Glyph {ch!r} lies outside the companion PNG bounds."
                )

            if ch in parsed:
                raise ValueError(f"Duplicate glyph entry for character {ch!r}.")

            parsed[ch] = (x, y, width)
            parsed_order.append(ch)

        if len(parsed_order) > 256:
            raise ValueError(
                "This editor can open at most 256 SignMatrix glyphs at once."
            )

        # Keep the space glyph at the Luminator-compatible 0x20 slot. Other
        # SignMatrix Unicode labels use adjacent internal slots and are kept
        # in display_labels rather than treated as byte codes.
        characters = ([" "] if " " in parsed else []) + [
            ch for ch in parsed_order if ch != " "
        ]
        first = 0x20
        last = first + len(characters) - 1
        count = len(characters)
        bytes_per_col = (height + 7) // 8

        obj = cls.__new__(cls)
        obj.path = json_path
        obj.raw = b""
        obj.height = height
        obj.spacing = spacing
        obj.first = first
        obj.last = last
        obj.count = count
        obj.bytes_per_col = bytes_per_col

        # Construct a conservative 28-byte Luminator header. The first 20
        # bytes are a human-readable descriptor; the structural bytes match
        # the layout observed in the supplied FNT files.
        prefix = bytearray(28)
        desc = str(metadata.get("desc") or json_path.stem)
        header_text = desc.upper().encode("ascii", "replace")[:20]
        prefix[:len(header_text)] = header_text
        prefix[22] = height
        prefix[23] = spacing
        prefix[24] = first
        prefix[25] = last
        prefix[26] = 0
        prefix[27] = 0
        obj.prefix = bytes(prefix)

        # Newly-created files use the standard N+1-offset form.
        obj.has_end_offset = True
        obj.end_offset = None
        obj.table_gap = b""
        obj.offsets = []
        obj.glyphs = []
        obj.display_labels = {}

        for code, ch in enumerate(characters, start=first):
            gx, gy, width = parsed[ch]
            obj.display_labels[code] = ch

            blob = bytearray(width * bytes_per_col)

            for x in range(width):
                for y in range(height):
                    pixel = image.pixelColor(gx + x, gy + y)

                    # Supplied SignMatrix sheets use transparent background
                    # and opaque white lit pixels. Treat any visible pixel
                    # as lit, which also tolerates differently colored sheets.
                    if pixel.alpha() > 0:
                        chunk = y // 8
                        stored_chunk = bytes_per_col - 1 - chunk
                        p = x * bytes_per_col + stored_chunk
                        blob[p] |= 1 << (y % 8)

            obj.glyphs.append(blob)

        return obj

    def width(self, idx):
        return len(self.glyphs[idx]) // self.bytes_per_col

    def pixel(self, idx, x, y):
        blob = self.glyphs[idx]
        # Luminator stores multi-byte vertical columns with the 8-row
        # chunks in reverse byte order.  Within each byte, bit 0 is the
        # top row of that 8-row chunk.
        chunk = y // 8
        stored_chunk = self.bytes_per_col - 1 - chunk
        p = x * self.bytes_per_col + stored_chunk
        return bool(blob[p] & (1 << (y % 8)))

    def set_pixel(self, idx, x, y, on):
        blob = self.glyphs[idx]
        chunk = y // 8
        stored_chunk = self.bytes_per_col - 1 - chunk
        p = x * self.bytes_per_col + stored_chunk
        mask = 1 << (y % 8)
        if on:
            blob[p] |= mask
        else:
            blob[p] &= (~mask) & 0xFF

    def bottom_rows_have_pixels(self, new_height):
        """Return True if shrinking to new_height would discard lit pixels."""
        new_height = int(new_height)
        if new_height >= self.height:
            return False
        if new_height < 1:
            return True

        for idx in range(len(self.glyphs)):
            width = self.width(idx)
            for y in range(new_height, self.height):
                for x in range(width):
                    if self.pixel(idx, x, y):
                        return True
        return False

    def resize_height(self, new_height):
        """Resize every glyph to the same height, anchored at the top.

        Increasing height appends blank rows at the bottom. Decreasing height
        removes bottom rows. The glyph storage is fully re-encoded because
        bytes-per-column changes whenever the height crosses an 8-pixel boundary.
        """
        new_height = int(new_height)
        if not 1 <= new_height <= 32:
            raise ValueError("Font height must be between 1 and 32 pixels.")
        if new_height == self.height:
            return

        old_height = self.height
        old_bytes_per_col = self.bytes_per_col

        # Capture the pixels that survive before changing the storage geometry.
        kept_height = min(old_height, new_height)
        glyph_pixels = []
        widths = []
        for idx, blob in enumerate(self.glyphs):
            width = len(blob) // old_bytes_per_col
            widths.append(width)
            pixels = [
                [self.pixel(idx, x, y) for x in range(width)]
                for y in range(kept_height)
            ]
            glyph_pixels.append(pixels)

        self.height = new_height
        self.bytes_per_col = (new_height + 7) // 8

        rebuilt = []
        for idx, width in enumerate(widths):
            blob = bytearray(width * self.bytes_per_col)
            rebuilt.append(blob)

        self.glyphs = rebuilt

        # Re-apply the surviving top-aligned pixels using the new chunk layout.
        for idx, pixels in enumerate(glyph_pixels):
            for y, row in enumerate(pixels):
                for x, on in enumerate(row):
                    if on:
                        self.set_pixel(idx, x, y, True)

    def add_glyph(self, code, width=1):
        """Extend the contiguous FNT character range to include code.

        Luminator FNT maps every byte value from first..last, so adding a
        character beyond the current edge may require blank placeholder glyphs
        for any intervening character codes.
        """
        code = int(code)
        if not 0 <= code <= 255:
            raise ValueError("Glyph character code must be between 0 and 255.")
        if self.first <= code <= self.last:
            raise ValueError(
                f"Character 0x{code:02X} is already inside this font's "
                "contiguous character range."
            )

        width = max(1, int(width))
        blank = bytearray(width * self.bytes_per_col)

        if code < self.first:
            # Prepend requested/intervening characters in ascending code order.
            new_glyphs = []
            for _c in range(code, self.first):
                new_glyphs.append(bytearray(blank))
            self.glyphs = new_glyphs + self.glyphs
            self.first = code
        else:
            # Append intervening characters through the requested code.
            for _c in range(self.last + 1, code + 1):
                self.glyphs.append(bytearray(blank))
            self.last = code

        self.count = self.last - self.first + 1

    def remove_edge_glyph(self, code):
        """Remove a glyph only when it is at the first/last range boundary."""
        code = int(code)
        if self.count <= 1:
            return False, "A font must contain at least one glyph."

        if code == self.first:
            del self.glyphs[0]
            self.first += 1
        elif code == self.last:
            self.glyphs.pop()
            self.last -= 1
        else:
            return False, (
                "Luminator FNT stores one contiguous character range from "
                "first to last. An interior glyph cannot be truly removed "
                "without changing the character codes of other glyphs."
            )

        self.count = self.last - self.first + 1
        return True, ""

    def add_column(self, idx, side="right"):
        blank = b"\x00" * self.bytes_per_col
        if side == "left":
            self.glyphs[idx] = bytearray(blank) + self.glyphs[idx]
        else:
            self.glyphs[idx].extend(blank)

    def remove_column(self, idx, side="right"):
        if self.width(idx) <= 1:
            return False

        if side == "left":
            del self.glyphs[idx][:self.bytes_per_col]
        else:
            del self.glyphs[idx][-self.bytes_per_col:]

        return True

    def _deployment_glyphs(self):
        """Return glyphs reordered by their labels for Luminator IPS typing."""
        labels = getattr(self, "display_labels", {})
        if not labels:
            return self.glyphs, self.first, self.last

        mapped = {}
        omitted = []
        for index, source_code in enumerate(range(self.first, self.last + 1)):
            label = " " if source_code == 0x20 else labels.get(
                source_code, display_character_for_code(source_code)
            )
            if len(label) != 1:
                raise ValueError(
                    f"Cannot export character 0x{source_code:02X}: "
                    f"{label!r} is not one character."
                )
            if not (0x20 <= ord(label) <= 0x7E):
                omitted.append(label)
                continue
            target_code = ord(label)
            if target_code in mapped:
                previous = mapped[target_code][0]
                raise ValueError(
                    f"Cannot export duplicate CP437 character {label!r}: "
                    f"source codes 0x{previous:02X} and 0x{source_code:02X}."
                )
            mapped[target_code] = (source_code, self.glyphs[index])

        # Luminator IPS expects the ordinary printable range to be present
        # at its ASCII byte positions, even when the source BBM omits a slot.
        first = 0x20
        last = 0x7E
        blank_width = max(1, self.width(0))
        blank = bytearray(blank_width * self.bytes_per_col)
        space = bytearray(3 * self.bytes_per_col)
        glyphs = [
            bytearray(mapped[code][1]) if code in mapped
            else bytearray(space) if code == 0x20
            else bytearray(blank)
            for code in range(first, last + 1)
        ]
        self.omitted_export_labels = sorted(set(omitted), key=ord)
        return glyphs, first, last

    def deployment_warnings(self):
        """Return non-ASCII BBM labels that will be blank in an FNT export."""
        self._deployment_glyphs()
        return list(getattr(self, "omitted_export_labels", []))

    def save(self, path):
        b = self.OFFSET_BASE
        glyphs, first, last = self._deployment_glyphs()
        count = last - first + 1
        table_size = count * 2 + (2 if self.has_end_offset else 0)
        current = table_size + len(self.table_gap)
        offsets = []

        for blob in glyphs:
            offsets.append(current)
            current += len(blob)
            if current > 0xFFFF:
                raise ValueError("Font became too large for the 16-bit offset table.")

        final_end = current

        out = bytearray(self.prefix)
        if len(out) > 23:
            out[22] = self.height & 0xFF
            out[23] = self.spacing & 0xFF
            out[24] = first & 0xFF
            out[25] = last & 0xFF

        # IMPORTANT: Luminator offsets are big-endian.
        for off in offsets:
            out += int(off).to_bytes(2, "big")

        if self.has_end_offset:
            out += int(final_end).to_bytes(2, "big")

        out += self.table_gap
        for blob in glyphs:
            out += blob

        Path(path).write_bytes(out)


class HanoverFont:
    """Hanover HELEN text FNT: 224 vertical 32-bit-column glyphs with widths."""

    FIRST_CODE = 0x20
    LAST_CODE = 0xFF
    GLYPH_COUNT = LAST_CODE - FIRST_CODE + 1

    def __init__(self, path):
        self.path = Path(path)
        try:
            with self.path.open("r", encoding="ascii", newline="") as source:
                text = source.read()
            self.newline = "\r\n" if "\r\n" in text else "\n"
            lines = text.splitlines()
            header = [int(value) for value in lines[0].split()]
            widths = [int(value) for value in lines[1].split()]
        except (IndexError, UnicodeDecodeError, ValueError) as e:
            raise ValueError("File is not a valid Hanover FNT text font.") from e

        if len(header) != 5 or header[0] not in (0, 2):
            raise ValueError("Unsupported Hanover FNT header.")
        if len(widths) != self.GLYPH_COUNT:
            raise ValueError(
                f"Hanover FNT must contain {self.GLYPH_COUNT} character widths."
            )

        self.version, self.height, self.max_width, self.character_set, self.spacing = header
        if not 1 <= self.height <= 32:
            raise ValueError("Hanover FNT height must be between 1 and 32 pixels.")
        if not 1 <= self.max_width <= 32:
            raise ValueError("Hanover FNT maximum glyph width must be between 1 and 32 pixels.")
        if not all(0 <= width <= self.max_width for width in widths):
            raise ValueError("Hanover FNT character widths exceed the maximum glyph width.")

        rows = lines[2:]
        if len(rows) != self.GLYPH_COUNT:
            raise ValueError(
                f"Hanover FNT must contain {self.GLYPH_COUNT} glyph rows."
            )
        try:
            words = [[int(value) for value in row.split()] for row in rows]
        except ValueError as e:
            raise ValueError("Hanover FNT bitmap data must be signed decimal integers.") from e
        if any(len(row) != self.max_width for row in words):
            raise ValueError("Each Hanover FNT glyph row must contain one value per bitmap column.")
        if any(value < -0x80000000 or value > 0x7FFFFFFF for row in words for value in row):
            raise ValueError("Hanover FNT bitmap values must fit in signed 32 bits.")

        # A glyph record contains exactly the header's maximum number of
        # bitmap columns. Some final columns happen to equal the width table,
        # but they remain genuine pixel data (not a width footer).
        self.has_width_footer = False
        self.bitmap_columns = self.max_width
        self.advance_widths = list(widths)

        self.first = self.FIRST_CODE
        self.last = self.LAST_CODE
        self.count = self.GLYPH_COUNT
        self.bytes_per_col = (self.height + 7) // 8
        self.glyphs = []
        for width, glyph_rows in zip(widths, words):
            glyph = bytearray(width * self.bytes_per_col)
            for x, signed_word in enumerate(glyph_rows[:width]):
                word = signed_word & 0xFFFFFFFF
                for y in range(self.height):
                    if word & (1 << y):
                        self._set_pixel_in_blob(glyph, x, y, True)
            self.glyphs.append(glyph)
        self.display_labels = hanover_display_labels()

    def _set_pixel_in_blob(self, blob, x, y, on):
        stored_chunk = self.bytes_per_col - 1 - (y // 8)
        offset = x * self.bytes_per_col + stored_chunk
        mask = 1 << (y % 8)
        if on:
            blob[offset] |= mask
        else:
            blob[offset] &= (~mask) & 0xFF

    def width(self, idx):
        return len(self.glyphs[idx]) // self.bytes_per_col

    def pixel(self, idx, x, y):
        stored_chunk = self.bytes_per_col - 1 - (y // 8)
        offset = x * self.bytes_per_col + stored_chunk
        return bool(self.glyphs[idx][offset] & (1 << (y % 8)))

    def set_pixel(self, idx, x, y, on):
        self._set_pixel_in_blob(self.glyphs[idx], x, y, on)

    def bottom_rows_have_pixels(self, new_height):
        return any(
            self.pixel(idx, x, y)
            for idx in range(self.count)
            for y in range(new_height, self.height)
            for x in range(self.width(idx))
        )

    def resize_height(self, new_height):
        if not 1 <= new_height <= 32:
            raise ValueError("Hanover FNT height must be between 1 and 32 pixels.")
        if new_height == self.height:
            return
        old_glyphs = self.glyphs
        old_height = self.height
        old_bytes_per_col = self.bytes_per_col
        self.height = new_height
        self.bytes_per_col = (new_height + 7) // 8
        self.glyphs = []
        for old_blob in old_glyphs:
            width = len(old_blob) // old_bytes_per_col
            glyph = bytearray(width * self.bytes_per_col)
            for y in range(min(old_height, new_height)):
                for x in range(width):
                    stored_chunk = old_bytes_per_col - 1 - (y // 8)
                    if old_blob[x * old_bytes_per_col + stored_chunk] & (1 << (y % 8)):
                        self._set_pixel_in_blob(glyph, x, y, True)
            self.glyphs.append(glyph)

    def add_column(self, idx, side="right"):
        if self.width(idx) >= self.bitmap_columns:
            raise ValueError("Hanover FNT glyphs cannot exceed 32 pixels wide.")
        blank = b"\x00" * self.bytes_per_col
        if side == "left":
            self.glyphs[idx] = bytearray(blank) + self.glyphs[idx]
        else:
            self.glyphs[idx].extend(blank)
        self.max_width = max(self.max_width, self.width(idx))
        self.advance_widths[idx] = self.width(idx)

    def remove_column(self, idx, side="right"):
        if self.width(idx) <= 0:
            return False
        if side == "left":
            del self.glyphs[idx][:self.bytes_per_col]
        else:
            del self.glyphs[idx][-self.bytes_per_col:]
        self.advance_widths[idx] = self.width(idx)
        return True

    def deployment_warnings(self):
        return []

    def save(self, path):
        header = (self.version, self.height, self.max_width, self.character_set, self.spacing)
        lines = [" ".join(map(str, header))]
        lines.append(" ".join(map(str, self.advance_widths)))
        for idx in range(self.count):
            values = []
            for x in range(self.max_width):
                word = sum(1 << y for y in range(self.height) if x < self.width(idx) and self.pixel(idx, x, y))
                values.append(str(word if word < 0x80000000 else word - 0x100000000))
            lines.append(" ".join(values))
        with Path(path).open("w", encoding="ascii", newline="") as destination:
            destination.write(self.newline.join(lines) + self.newline)



class PixelCanvas(QWidget):
    changed = pyqtSignal()
    changeStarted = pyqtSignal()
    selectionChanged = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.font_model = None
        self.glyph_index = 0
        self.base_cell_size = 36
        self.scale_factor = 1.0
        self.cell_size = self.base_cell_size
        self.paint_value = None
        self.last_cell = None

        # Hover/press tracking purely for visual feedback while painting.
        self.hover_cell = None
        self.active_cells = set()

        # Selection / move state.
        # Right-drag creates a rectangular selection. Left-dragging from
        # inside that selection moves the selected pixels as a group.
        self.selection = None          # (left, top, right, bottom), inclusive
        self.selection_anchor = None   # right-drag start cell
        self.selecting = False

        self.moving_selection = False
        self.move_start_cell = None
        self.move_original_rect = None
        self.move_original_bitmap = None
        self.move_current_delta = (0, 0)

        self.setMouseTracking(True)
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)

    def set_scale_factor(self, factor):
        factor = max(0.15, min(4.0, float(factor)))
        self.scale_factor = factor
        self.cell_size = max(6, int(round(self.base_cell_size * factor)))
        self.update_geometry()
        self.update()

    def set_font_model(self, model):
        self.font_model = model
        self.glyph_index = 0
        self.hover_cell = None
        self.active_cells = set()
        self.clear_selection()
        self.update_geometry()
        self.update()

    def set_glyph_index(self, idx):
        self.glyph_index = idx
        self.hover_cell = None
        self.active_cells = set()
        self.clear_selection()
        self.update_geometry()
        self.update()

    def update_geometry(self):
        if not self.font_model:
            self.setFixedSize(300, 240)
            return
        w = max(1, self.font_model.width(self.glyph_index))
        h = self.font_model.height
        self.setFixedSize(w * self.cell_size + 1, h * self.cell_size + 1)

    def sizeHint(self):
        return self.size()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, False)

        # LED-style preview: dark unlit dots, bright lit dots.
        bg = QColor("#070B12")
        off = QColor("#202938")
        on = QColor("#F8FAFC")
        cell_border = QColor("#172033")

        painter.fillRect(self.rect(), bg)

        if not self.font_model:
            painter.setPen(QColor("#94A3B8"))
            painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, "Open a .fnt file to begin")
            return

        cols = self.font_model.width(self.glyph_index)
        rows = self.font_model.height

        # Leave some breathing room around each LED so the matrix reads
        # like a physical dot display rather than a spreadsheet grid.
        dot_margin = max(3, int(self.cell_size * 0.16))
        dot_size = self.cell_size - (dot_margin * 2)

        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)

        # Individual pixel hover/press feedback is only shown while there is
        # no active selection box, per requirement 2.
        show_pixel_feedback = self.selection is None

        for y in range(rows):
            for x in range(cols):
                cell_x = x * self.cell_size
                cell_y = y * self.cell_size
                is_on = self.font_model.pixel(self.glyph_index, x, y)

                # Very subtle cell boundary to make targeting pixels easier.
                painter.setPen(QPen(cell_border, 1))
                painter.setBrush(Qt.BrushStyle.NoBrush)
                painter.drawRect(cell_x, cell_y, self.cell_size, self.cell_size)

                if show_pixel_feedback and (x, y) in self.active_cells:
                    painter.setBrush(QColor(59, 130, 246, 90))
                    painter.drawRect(cell_x + 1, cell_y + 1, self.cell_size - 2, self.cell_size - 2)
                elif show_pixel_feedback and (x, y) == self.hover_cell:
                    painter.setBrush(QColor(148, 163, 184, 45))
                    painter.drawRect(cell_x + 1, cell_y + 1, self.cell_size - 2, self.cell_size - 2)

                # Circular LED dot.
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(on if is_on else off)
                painter.drawEllipse(
                    cell_x + dot_margin,
                    cell_y + dot_margin,
                    dot_size,
                    dot_size
                )

        # Photoshop-style rectangular selection overlay.
        if self.selection is not None:
            left, top, right, bottom = self.selection
            x = left * self.cell_size
            y = top * self.cell_size
            w = (right - left + 1) * self.cell_size
            h = (bottom - top + 1) * self.cell_size

            hovering_selection = (
                self.hover_cell is not None
                and not self.selecting
                and self._cell_in_selection(self.hover_cell)
            )

            if self.moving_selection:
                fill = QColor(59, 130, 246, 80)
                border = QColor("#93C5FD")
            elif hovering_selection:
                fill = QColor(59, 130, 246, 50)
                border = QColor("#7DB6FA")
            else:
                fill = QColor(59, 130, 246, 28)
                border = QColor("#60A5FA")

            painter.setBrush(fill)
            selection_pen = QPen(border, 2)
            selection_pen.setStyle(
                Qt.PenStyle.SolidLine if self.moving_selection else Qt.PenStyle.DashLine
            )
            painter.setPen(selection_pen)
            painter.drawRect(x + 1, y + 1, max(1, w - 2), max(1, h - 2))

    def _cell_from_pos(self, pos):
        if not self.font_model:
            return None
        x = int(pos.x() // self.cell_size)
        y = int(pos.y() // self.cell_size)
        if 0 <= x < self.font_model.width(self.glyph_index) and 0 <= y < self.font_model.height:
            return (x, y)
        return None

    def _paint_cell(self, cell):
        if cell is None or cell == self.last_cell or self.paint_value is None:
            return
        x, y = cell
        self.font_model.set_pixel(self.glyph_index, x, y, self.paint_value)
        self.last_cell = cell
        self.active_cells.add(cell)
        self.update()

    def clear_selection(self):
        had_selection = self.selection is not None
        self.selection = None
        self.selection_anchor = None
        self.selecting = False
        self.moving_selection = False
        self.move_start_cell = None
        self.move_original_rect = None
        self.move_original_bitmap = None
        self.move_current_delta = (0, 0)
        self.update()
        if had_selection:
            self.selectionChanged.emit()

    def _normalized_selection(self, a, b):
        if a is None or b is None:
            return None
        x1, y1 = a
        x2, y2 = b
        return (min(x1, x2), min(y1, y2), max(x1, x2), max(y1, y2))

    def _cell_in_selection(self, cell):
        if cell is None or self.selection is None:
            return False
        x, y = cell
        left, top, right, bottom = self.selection
        return left <= x <= right and top <= y <= bottom

    def _capture_selection_bitmap(self):
        if not self.font_model or self.selection is None:
            return None
        left, top, right, bottom = self.selection
        return [
            [
                self.font_model.pixel(self.glyph_index, x, y)
                for x in range(left, right + 1)
            ]
            for y in range(top, bottom + 1)
        ]

    def _apply_selection_move(self, dx, dy):
        """Move the selected rectangular bitmap, clipping at glyph edges."""
        if (
            not self.font_model
            or self.move_original_rect is None
            or self.move_original_bitmap is None
        ):
            return

        cols = self.font_model.width(self.glyph_index)
        rows = self.font_model.height
        left, top, right, bottom = self.move_original_rect

        # Clear the original selected rectangle first. This makes moving behave
        # like a cut-and-move operation rather than a copy.
        for y in range(top, bottom + 1):
            for x in range(left, right + 1):
                self.font_model.set_pixel(self.glyph_index, x, y, False)

        # Paste the original bitmap at its new location. Both lit and unlit
        # pixels in the selected rectangle are moved, matching image-editor
        # rectangular-selection behavior.
        for sy, row_bits in enumerate(self.move_original_bitmap):
            for sx, value in enumerate(row_bits):
                tx = left + sx + dx
                ty = top + sy + dy
                if 0 <= tx < cols and 0 <= ty < rows:
                    self.font_model.set_pixel(self.glyph_index, tx, ty, value)

        new_left = left + dx
        new_top = top + dy
        new_right = right + dx
        new_bottom = bottom + dy

        # The visible selection rectangle is clipped to the glyph bounds.
        clipped_left = max(0, new_left)
        clipped_top = max(0, new_top)
        clipped_right = min(cols - 1, new_right)
        clipped_bottom = min(rows - 1, new_bottom)

        if clipped_left <= clipped_right and clipped_top <= clipped_bottom:
            self.selection = (
                clipped_left,
                clipped_top,
                clipped_right,
                clipped_bottom,
            )
        else:
            self.selection = None

        self.move_current_delta = (dx, dy)
        self.update()
        self.selectionChanged.emit()

    def mousePressEvent(self, event):
        if not self.font_model:
            return

        cell = self._cell_from_pos(event.position())

        # Right button: start a rectangular pixel selection.
        if event.button() == Qt.MouseButton.RightButton:
            if cell is None:
                self.clear_selection()
                event.accept()
                return

            self.selection_anchor = cell
            self.selection = (cell[0], cell[1], cell[0], cell[1])
            self.selecting = True
            self.paint_value = None
            self.last_cell = None
            self.update()
            self.selectionChanged.emit()
            event.accept()
            return

        if event.button() != Qt.MouseButton.LeftButton:
            return

        # Clicking outside the pixel grid entirely just cancels any selection.
        if cell is None:
            if self.selection is not None:
                self.clear_selection()
            event.accept()
            return

        # Left-drag from inside a selection: move the selected pixels.
        if self._cell_in_selection(cell):
            self.changeStarted.emit()
            self.moving_selection = True
            self.move_start_cell = cell
            self.move_original_rect = self.selection
            self.move_original_bitmap = self._capture_selection_bitmap()
            self.move_current_delta = (0, 0)
            self.paint_value = None
            self.last_cell = None
            self.update()
            event.accept()
            return

        # Clicking an unselected pixel while a selection is active only
        # cancels the selection; it must not also toggle that pixel.
        if self.selection is not None:
            self.clear_selection()
            event.accept()
            return

        self.changeStarted.emit()
        x, y = cell
        self.paint_value = not self.font_model.pixel(self.glyph_index, x, y)
        self.last_cell = None
        self.active_cells = set()
        self._paint_cell(cell)
        event.accept()

    def mouseMoveEvent(self, event):
        cell = self._cell_from_pos(event.position())

        if cell != self.hover_cell:
            self.hover_cell = cell
            self.update()

        # Update rectangular right-drag selection.
        if self.selecting and (event.buttons() & Qt.MouseButton.RightButton):
            if cell is not None:
                self.selection = self._normalized_selection(
                    self.selection_anchor, cell
                )
                self.update()
                self.selectionChanged.emit()
            event.accept()
            return

        # Move the captured selection one cell at a time.
        if self.moving_selection and (event.buttons() & Qt.MouseButton.LeftButton):
            if cell is not None and self.move_start_cell is not None:
                dx = cell[0] - self.move_start_cell[0]
                dy = cell[1] - self.move_start_cell[1]
                if (dx, dy) != self.move_current_delta:
                    # Restore the original selected bitmap before applying the
                    # new delta, so dragging remains stable instead of smearing.
                    if self.move_original_rect is not None:
                        left, top, right, bottom = self.move_original_rect
                        bitmap = self.move_original_bitmap

                        # First clear the current glyph region affected by the
                        # previous preview by reconstructing from the original
                        # state of the selected rectangle and current move.
                        old_dx, old_dy = self.move_current_delta
                        cols = self.font_model.width(self.glyph_index)
                        rows = self.font_model.height

                        # Clear previous destination.
                        for sy, row_bits in enumerate(bitmap):
                            for sx, _ in enumerate(row_bits):
                                px = left + sx + old_dx
                                py = top + sy + old_dy
                                if 0 <= px < cols and 0 <= py < rows:
                                    self.font_model.set_pixel(
                                        self.glyph_index, px, py, False
                                    )

                        # Restore original selection contents.
                        for sy, row_bits in enumerate(bitmap):
                            for sx, value in enumerate(row_bits):
                                px = left + sx
                                py = top + sy
                                if 0 <= px < cols and 0 <= py < rows:
                                    self.font_model.set_pixel(
                                        self.glyph_index, px, py, value
                                    )

                    self._apply_selection_move(dx, dy)
            event.accept()
            return

        if event.buttons() & Qt.MouseButton.LeftButton:
            self._paint_cell(cell)

    def leaveEvent(self, event):
        if self.hover_cell is not None:
            self.hover_cell = None
            self.update()
        super().leaveEvent(event)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.RightButton and self.selecting:
            self.selecting = False
            self.selection_anchor = None
            self.update()
            event.accept()
            return

        if event.button() == Qt.MouseButton.LeftButton and self.moving_selection:
            moved = self.move_current_delta != (0, 0)
            self.moving_selection = False
            self.move_start_cell = None
            self.move_original_rect = None
            self.move_original_bitmap = None
            self.move_current_delta = (0, 0)
            if moved:
                self.changed.emit()
            self.update()
            event.accept()
            return

        had_paint = self.paint_value is not None
        self.paint_value = None
        self.last_cell = None
        self.active_cells = set()
        if had_paint:
            self.changed.emit()
            self.update()


class CharacterMapDelegate(QStyledItemDelegate):
    """Paint Character Map cells ourselves so populated-glyph colors cannot
    be overridden by Qt's stylesheet engine.
    """

    POPULATED_ROLE = int(Qt.ItemDataRole.UserRole) + 1

    def paint(self, painter, option, index):
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)

        rect = option.rect.adjusted(2, 2, -2, -2)
        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        hovered = bool(option.state & QStyle.StateFlag.State_MouseOver)
        populated = bool(index.data(self.POPULATED_ROLE))

        if selected:
            bg = QColor("#2563EB")
            fg = QColor("#FFFFFF")
            border = QColor("#60A5FA")
        elif populated:
            # Deliberately visible but still subdued against the dark UI.
            bg = QColor("#285A3A")
            fg = QColor("#E4F7E9")
            border = QColor("#3D7650")
        elif hovered:
            bg = QColor("#22304A")
            fg = QColor("#E5E7EB")
            border = QColor("#34435C")
        else:
            bg = QColor("#172033")
            fg = QColor("#E5E7EB")
            border = QColor("#263449")

        painter.setPen(QPen(border, 1))
        painter.setBrush(bg)
        painter.drawRoundedRect(rect, 5, 5)

        painter.setPen(fg)
        painter.drawText(
            rect,
            int(Qt.AlignmentFlag.AlignCenter),
            str(index.data(Qt.ItemDataRole.DisplayRole) or "")
        )
        painter.restore()


def display_character_for_code(code):
    """Return a visible label for a one-byte font character code."""
    code = int(code) & 0xFF
    character = bytes([code]).decode("cp437")
    if code == 0x20:
        return "SPACE"
    if code < 0x20 or code == 0x7F:
        return f"0x{code:02X}"
    return character


def hanover_display_labels():
    """Return the HELEN single-byte character map (Windows-1252)."""
    labels = {}
    for code in range(HanoverFont.FIRST_CODE, HanoverFont.LAST_CODE + 1):
        try:
            labels[code] = bytes([code]).decode("cp1252")
        except UnicodeDecodeError:
            # Windows-1252 reserves these byte positions; HELEN leaves them blank.
            labels[code] = ""
    labels[0x7F] = ""
    return labels


def bbm_display_labels(first, count):
    """Return confirmed Axion BBM labels without changing numeric glyph codes."""
    labels = {
        code: display_character_for_code(code)
        for code in range(first, first + count)
    }
    for index, character in enumerate("abcdefghijklmnopqrstuvwxyz"):
        code = 0x5F + index
        if first <= code < first + count:
            labels[code] = character

    # The TRU files use a custom byte-to-character table after lowercase z.
    # Keep each correction explicit: neither CP437 nor Unicode ordering matches.
    extended = {
        0x2A: "Î",
        0x3C: "Û",
        0x3E: "Ù",
        0x5B: "Ü",
        0x5D: "Ô",
        0x5E: "_",
        0x79: "Ç",
        0x7A: "ü",
        0x7B: "é",
        0x7C: "â",
        0x7D: "Â",
        0x7E: "à",
        0x7F: "ç",
        0x80: "ê",
        0x81: "ë",
        0x82: "è",
        0x83: "ï",
        0x84: "î",
        0x85: "ì",
        0x86: "À",
        0x87: "É",
        0x88: "È",
        0x89: "Ê",
        0x8A: "ô",
        0x8B: "Ë",
        0x8C: "Ï",
        0x8D: "û",
        0x8E: "ù",
    }
    for code, character in extended.items():
        if first <= code < first + count:
            labels[code] = character
    return labels


def bbm_cp437_display_labels(first, count):
    """Return the corrected post-tilde labels for standard BBM files."""
    labels = {}
    post_tilde = "éâàçêëèïîìôûù¢½¼"
    for index, character in enumerate(post_tilde):
        code = 0x7F + index
        if first <= code < first + count:
            labels[code] = character
    return labels


class MainWindow(QMainWindow):
    SOURCE_FORMATS = (
        ("Luminator (.FNT)", "luminator"),
        ("Hanover (.FNT)", "hanover"),
        ("Axion (.BBM)", "bbm"),
        ("SignMatrix (.JSON+.PNG)", "signmatrix"),
    )
    TARGET_FORMATS = tuple(
        item for item in SOURCE_FORMATS if item[1] != "bbm"
    )

    def __init__(self):
        super().__init__()
        self.model = None
        self.current_path = None
        self.dirty = False
        self.manual_scale_override = False
        self.undo_stack = []
        self.redo_stack = []
        self._pending_history_snapshot = None
        self._restoring_history = False
        self._imported_from_signmatrix = False
        self._new_from_scratch = False
        self._selected_codes = []
        self._confirmed_save_paths = set()
        self.clipboard = None

        # Each file/folder dialog remembers its own last-used location.
        # QSettings keeps these paths across application restarts.
        self.settings = QSettings("LuminatorFontEditor", "FNTEditor")

        self.setWindowTitle("Luminator FNT Pixel Editor")
        self.resize(980, 760)

        self._build_toolbar()
        self._build_ui()
        self._apply_style()
        self.update_undo_redo_buttons()

        self.statusBar().showMessage("Ready")

    def _build_toolbar(self):
        toolbar = QToolBar("Main")
        toolbar.setMovable(False)
        toolbar.setIconSize(QSize(18, 18))
        self.addToolBar(toolbar)

        new_action = QAction("New", self)
        new_action.setShortcut("Ctrl+N")
        new_action.setToolTip("Create a blank Luminator FNT font from scratch")
        new_action.triggered.connect(self.create_new_font)
        toolbar.addAction(new_action)

        open_action = QAction("Open", self)
        open_action.setShortcut("Ctrl+O")
        self.addAction(open_action)
        open_menu = QMenu(self)
        open_menu.addAction(
            "Luminator (.FNT)", lambda: self.open_file("luminator")
        )
        open_menu.addAction(
            "Hanover (.FNT)", lambda: self.open_file("hanover")
        )
        open_menu.addAction("Axion (.BBM)", lambda: self.open_file("bbm"))
        open_menu.addAction(
            "SignMatrix (.JSON+.PNG)", lambda: self.open_file("signmatrix")
        )
        open_button = QToolButton()
        open_button.setDefaultAction(open_action)
        open_button.setMenu(open_menu)
        open_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        open_button.setToolTip("Open a font format")
        toolbar.addWidget(open_button)

        save_action = QAction("Save", self)
        save_action.setShortcut("Ctrl+S")
        save_action.triggered.connect(self.save)
        save_action.setEnabled(False)
        self.save_action = save_action
        toolbar.addAction(save_action)

        save_as_action = QAction("Save As", self)
        save_as_action.setShortcut("Ctrl+Shift+S")
        save_as_action.triggered.connect(self.save_as)
        save_as_action.setEnabled(False)
        self.save_as_action = save_as_action
        self.addAction(save_as_action)
        save_as_menu = QMenu(self)
        save_as_menu.addAction(
            "Luminator (.FNT)", lambda: self.save_as("luminator")
        )
        save_as_menu.addAction(
            "Hanover (.FNT)", lambda: self.save_as("hanover")
        )
        save_as_menu.addAction(
            "SignMatrix (.JSON+.PNG)", lambda: self.save_as("signmatrix")
        )
        save_as_button = QToolButton()
        save_as_button.setDefaultAction(save_as_action)
        save_as_button.setMenu(save_as_menu)
        save_as_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        save_as_button.setToolTip("Save the font in a chosen format")
        toolbar.addWidget(save_as_button)

        toolbar.addSeparator()

        batch_convert_action = QAction("Mass Convert", self)
        batch_convert_action.setToolTip("Convert multiple font files into another supported format")
        batch_convert_action.triggered.connect(self.mass_convert_fonts)
        toolbar.addAction(batch_convert_action)

        extract_action = QAction("Extract FNTs from IPS", self)
        extract_action.setToolTip("Extract all embedded .fnt files from a Luminator .ips database")
        extract_action.triggered.connect(self.extract_fnts_from_ips)
        toolbar.addAction(extract_action)

        toolbar.addSeparator()

        self.filename_label = QLabel("No font loaded")
        self.filename_label.setObjectName("FilenameLabel")
        toolbar.addWidget(self.filename_label)

    def _build_ui(self):
        root = QWidget()
        self.setCentralWidget(root)

        main = QHBoxLayout(root)
        main.setContentsMargins(20, 20, 20, 20)
        main.setSpacing(18)

        # Left: controls
        side = QFrame()
        side.setObjectName("SidePanel")
        side.setFixedWidth(280)
        side_layout = QVBoxLayout(side)
        side_layout.setContentsMargins(18, 18, 18, 18)
        side_layout.setSpacing(14)

        title = QLabel("Font Controls")
        title.setObjectName("PanelTitle")
        side_layout.addWidget(title)

        glyph_group = QGroupBox("Glyph")
        glyph_layout = QGridLayout(glyph_group)

        glyph_layout.addWidget(QLabel("Selected"), 0, 0)
        self.selected_char_label = QLabel("—")
        self.selected_char_label.setObjectName("SelectedCharLabel")
        glyph_layout.addWidget(self.selected_char_label, 0, 1)

        glyph_layout.addWidget(QLabel("Letter spacing"), 1, 0)
        self.spacing_spin = QSpinBox()
        self.spacing_spin.setRange(0, 9)
        self.spacing_spin.valueChanged.connect(self.spacing_changed)
        glyph_layout.addWidget(self.spacing_spin, 1, 1)

        side_layout.addWidget(glyph_group)

        char_group = QGroupBox("Character Map")
        char_layout = QVBoxLayout(char_group)

        self.char_table = QTableWidget(0, 1)
        self.char_table.setObjectName("CharacterTable")
        self.char_table.setItemDelegate(CharacterMapDelegate(self.char_table))
        self.char_table.horizontalHeader().setVisible(False)
        self.char_table.verticalHeader().setVisible(False)
        self.char_table.setShowGrid(False)
        self.char_table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.char_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectItems)
        self.char_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.char_table.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.char_table.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.char_table.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.char_table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.Stretch
        )
        self.char_table.itemSelectionChanged.connect(self.character_selection_changed)
        self.char_table.setFixedHeight(250)
        self._char_columns = 1
        self._char_cell_min_width = 28
        self.char_table.viewport().installEventFilter(self)

        char_layout.addWidget(self.char_table)
        side_layout.addWidget(char_group)

        metrics_group = QGroupBox("Metrics")
        metrics_layout = QGridLayout(metrics_group)
        self.height_value = QSpinBox()
        self.height_value.setRange(1, 32)
        self.height_value.setSuffix(" px")
        self.height_value.setEnabled(False)
        self.height_value.setToolTip(
            "Changes the height of every glyph. Rows are added or removed at the bottom."
        )
        self.height_value.valueChanged.connect(self.font_height_changed)

        self.width_value = QLabel("—")
        self.bytes_value = QLabel("—")
        metrics_layout.addWidget(QLabel("Height"), 0, 0)
        metrics_layout.addWidget(self.height_value, 0, 1)
        metrics_layout.addWidget(QLabel("Width"), 1, 0)
        metrics_layout.addWidget(self.width_value, 1, 1)
        metrics_layout.addWidget(QLabel("Bytes/column"), 2, 0)
        metrics_layout.addWidget(self.bytes_value, 2, 1)
        side_layout.addWidget(metrics_group)

        edit_group = QGroupBox("Edit Glyph")
        edit_layout = QVBoxLayout(edit_group)

        add_row = QHBoxLayout()

        self.add_left_col_btn = QPushButton("Add Left")
        self.add_left_col_btn.setToolTip("Add a blank column to the left side of this glyph")
        self.add_left_col_btn.clicked.connect(self.add_left_column)
        add_row.addWidget(self.add_left_col_btn)

        self.add_right_col_btn = QPushButton("Add Right")
        self.add_right_col_btn.setToolTip("Add a blank column to the right side of this glyph")
        self.add_right_col_btn.clicked.connect(self.add_right_column)
        add_row.addWidget(self.add_right_col_btn)

        edit_layout.addLayout(add_row)

        remove_row = QHBoxLayout()

        self.remove_left_col_btn = QPushButton("Remove Left")
        self.remove_left_col_btn.setToolTip("Remove the leftmost column of this glyph")
        self.remove_left_col_btn.clicked.connect(self.remove_left_column)
        remove_row.addWidget(self.remove_left_col_btn)

        self.remove_right_col_btn = QPushButton("Remove Right")
        self.remove_right_col_btn.setToolTip("Remove the rightmost column of this glyph")
        self.remove_right_col_btn.clicked.connect(self.remove_right_column)
        remove_row.addWidget(self.remove_right_col_btn)

        edit_layout.addLayout(remove_row)

        glyph_row = QHBoxLayout()

        self.add_glyph_btn = QPushButton("Add Glyph")
        self.add_glyph_btn.setToolTip(
            "Add a character to the font's first/last character range"
        )
        self.add_glyph_btn.clicked.connect(self.add_glyph)
        glyph_row.addWidget(self.add_glyph_btn)

        self.remove_glyph_btn = QPushButton("Remove Glyph")
        self.remove_glyph_btn.setObjectName("DangerButton")
        self.remove_glyph_btn.setToolTip(
            "Remove the selected glyph when it is at an edge of the FNT character range"
        )
        self.remove_glyph_btn.clicked.connect(self.remove_glyph)
        glyph_row.addWidget(self.remove_glyph_btn)

        edit_layout.addLayout(glyph_row)

        self.clear_btn = QPushButton("Clear Glyph")
        self.clear_btn.setObjectName("DangerButton")
        self.clear_btn.clicked.connect(self.clear_glyph)
        edit_layout.addWidget(self.clear_btn)

        side_layout.addWidget(edit_group)
        side_layout.addStretch(1)

        hint = QLabel("Tip: left-drag paints or erases. Right-drag selects a rectangle; then left-drag inside the selection to move those pixels as a group. Ctrl + mouse wheel adjusts scale.")
        hint.setWordWrap(True)
        hint.setObjectName("HintLabel")
        side_layout.addWidget(hint)

        main.addWidget(side)

        # Right: editor
        editor_panel = QFrame()
        editor_panel.setObjectName("EditorPanel")
        editor_layout = QVBoxLayout(editor_panel)
        editor_layout.setContentsMargins(18, 18, 18, 18)
        editor_layout.setSpacing(12)

        header_row = QHBoxLayout()
        self.glyph_title = QLabel("Pixel Editor")
        self.glyph_title.setObjectName("EditorTitle")
        header_row.addWidget(self.glyph_title)

        self.undo_btn = QPushButton()
        self.undo_btn.setObjectName("CompactButton")
        self.undo_btn.setIcon(_svg_icon(_UNDO_SVG))
        self.undo_btn.setIconSize(QSize(18, 18))
        self.undo_btn.setToolTip("Undo (Ctrl+Z)")
        self.undo_btn.clicked.connect(self.undo)
        header_row.addWidget(self.undo_btn)

        self.redo_btn = QPushButton()
        self.redo_btn.setObjectName("CompactButton")
        self.redo_btn.setIcon(_svg_icon(_REDO_SVG))
        self.redo_btn.setIconSize(QSize(18, 18))
        self.redo_btn.setToolTip("Redo (Ctrl+Y)")
        self.redo_btn.clicked.connect(self.redo)
        header_row.addWidget(self.redo_btn)

        header_row.addStretch(1)

        scale_label = QLabel("Scale")
        scale_label.setObjectName("ScaleLabel")
        header_row.addWidget(scale_label)

        self.scale_slider = QSlider(Qt.Orientation.Horizontal)
        self.scale_slider.setRange(15, 400)
        self.scale_slider.setValue(100)
        self.scale_slider.setFixedWidth(180)
        self.scale_slider.setToolTip("Zoom the pixel editor")
        self.scale_slider.valueChanged.connect(self.manual_scale_changed)
        header_row.addWidget(self.scale_slider)

        self.scale_value_label = QLabel("100%")
        self.scale_value_label.setObjectName("ScaleValueLabel")
        self.scale_value_label.setFixedWidth(46)
        self.scale_value_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        header_row.addWidget(self.scale_value_label)

        self.fit_btn = QPushButton("Fit")
        self.fit_btn.setObjectName("CompactButton")
        self.fit_btn.clicked.connect(self.enable_auto_fit)
        header_row.addWidget(self.fit_btn)

        editor_layout.addLayout(header_row)

        self.canvas = PixelCanvas()
        self.canvas.changeStarted.connect(self._begin_history_action)
        self.canvas.changed.connect(self._canvas_change_finished)
        self.canvas.selectionChanged.connect(self._update_selection_actions_bar)
        self.canvas.installEventFilter(self)

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(False)
        self.scroll.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.scroll.setWidget(self.canvas)
        self.scroll.setObjectName("CanvasScroll")
        self.scroll.viewport().installEventFilter(self)
        self.scroll.installEventFilter(self)
        editor_layout.addWidget(self.scroll, 1)

        # Floating buttons overlaid on top of the canvas viewport. Copy/Paste
        # are always available; Set Lit/Unlit only apply to a selection.
        self.selection_actions_bar = QWidget(self.scroll)
        self.selection_actions_bar.setObjectName("SelectionActionsBar")
        self.selection_actions_bar.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        selection_actions_layout = QHBoxLayout(self.selection_actions_bar)
        selection_actions_layout.setContentsMargins(10, 6, 10, 6)
        selection_actions_layout.setSpacing(8)

        self.copy_btn = QPushButton("Copy")
        self.copy_btn.setObjectName("SelectionActionButton")
        self.copy_btn.setToolTip("Copy the selection (or the whole glyph if nothing is selected)")
        self.copy_btn.clicked.connect(self.copy_pixels)
        selection_actions_layout.addWidget(self.copy_btn)

        self.paste_btn = QPushButton("Paste")
        self.paste_btn.setObjectName("SelectionActionButton")
        self.paste_btn.setToolTip("Paste into the selection (or the top-left corner if nothing is selected)")
        self.paste_btn.clicked.connect(self.paste_pixels)
        selection_actions_layout.addWidget(self.paste_btn)

        self.mirror_h_btn = QPushButton("Mirror H")
        self.mirror_h_btn.setObjectName("SelectionActionButton")
        self.mirror_h_btn.setToolTip(
            "Flip the selection (or the whole glyph if nothing is selected) left-to-right"
        )
        self.mirror_h_btn.clicked.connect(lambda: self.mirror_pixels("horizontal"))
        selection_actions_layout.addWidget(self.mirror_h_btn)

        self.mirror_v_btn = QPushButton("Mirror V")
        self.mirror_v_btn.setObjectName("SelectionActionButton")
        self.mirror_v_btn.setToolTip(
            "Flip the selection (or the whole glyph if nothing is selected) top-to-bottom"
        )
        self.mirror_v_btn.clicked.connect(lambda: self.mirror_pixels("vertical"))
        selection_actions_layout.addWidget(self.mirror_v_btn)

        self.set_lit_btn = QPushButton("Set Lit")
        self.set_lit_btn.setObjectName("SelectionActionButton")
        self.set_lit_btn.setToolTip("Turn every pixel in the selection on")
        self.set_lit_btn.clicked.connect(lambda: self.set_selection_pixels(True))
        selection_actions_layout.addWidget(self.set_lit_btn)

        self.set_unlit_btn = QPushButton("Set Unlit")
        self.set_unlit_btn.setObjectName("SelectionActionButton")
        self.set_unlit_btn.setToolTip("Turn every pixel in the selection off")
        self.set_unlit_btn.clicked.connect(lambda: self.set_selection_pixels(False))
        selection_actions_layout.addWidget(self.set_unlit_btn)

        self.selection_actions_bar.setVisible(False)

        self.multi_select_label = QLabel("Multiple characters selected")
        self.multi_select_label.setObjectName("MultiSelectLabel")
        self.multi_select_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.multi_select_label.setVisible(False)
        editor_layout.addWidget(self.multi_select_label, 1)

        main.addWidget(editor_panel, 1)

    def _apply_style(self):
        self.setStyleSheet("""
            QMainWindow, QWidget {
                background: #0B1220;
                color: #E5E7EB;
            }

            QToolBar {
                background: #111827;
                color: #F8FAFC;
                border: none;
                border-bottom: 1px solid #253047;
                padding: 8px 12px;
                spacing: 8px;
            }

            QToolButton {
                background: #172033;
                color: #F8FAFC;
                border: 1px solid #334155;
                border-radius: 8px;
                padding: 7px 12px;
            }
            QToolButton:hover {
                background: #22304A;
            }
            QToolButton:pressed {
                background: #2B3B58;
            }
            QToolButton:disabled {
                background: #111827;
                color: #64748B;
                border-color: #253047;
            }
            QToolButton::menu-button {
                width: 24px;
                border: none;
            }
            QToolButton::menu-arrow {
                margin-left: 6px;
            }

            QMenu {
                background: #111827;
                color: #F8FAFC;
                border: 1px solid #334155;
                padding: 4px;
            }
            QMenu::item {
                min-height: 16px;
                padding: 7px 36px 7px 12px;
                border-radius: 5px;
            }
            QMenu::item:selected {
                background: #22304A;
            }
            QMenu::item:pressed {
                background: #2B3B58;
            }

            #FilenameLabel {
                background: transparent;
                color: #CBD5E1;
                padding-left: 8px;
            }

            #SidePanel, #EditorPanel {
                background: #111827;
                color: #E5E7EB;
                border: 1px solid #253047;
                border-radius: 14px;
            }

            #PanelTitle, #EditorTitle {
                background: transparent;
                color: #F8FAFC;
                font-size: 18px;
                font-weight: 700;
            }

            QGroupBox {
                background: #111827;
                color: #CBD5E1;
                font-weight: 600;
                border: 1px solid #253047;
                border-radius: 10px;
                margin-top: 12px;
                padding-top: 12px;
            }
            QGroupBox::title {
                subcontrol-origin: margin;
                left: 10px;
                padding: 0 5px;
                background: #111827;
                color: #CBD5E1;
            }

            QLabel {
                background: transparent;
                color: #CBD5E1;
            }

            #SelectedCharLabel {
                color: #F8FAFC;
                font-weight: 700;
            }

            QSpinBox {
                background: #0F172A;
                color: #F8FAFC;
                border: 1px solid #334155;
                border-radius: 7px;
                padding: 6px 8px;
                min-height: 24px;
                selection-background-color: #2563EB;
                selection-color: #FFFFFF;
            }
            QSpinBox:hover {
                border-color: #475569;
            }
            QSpinBox:focus {
                border: 1px solid #3B82F6;
            }

            QPushButton {
                background: #172033;
                color: #F8FAFC;
                border: 1px solid #334155;
                border-radius: 8px;
                padding: 8px 10px;
                text-align: left;
            }
            QPushButton:hover {
                background: #22304A;
            }
            QPushButton:pressed {
                background: #2B3B58;
            }
            QPushButton:disabled {
                background: #111827;
                color: #64748B;
                border-color: #253047;
            }

            #ScaleLabel {
                color: #94A3B8;
            }
            #ScaleValueLabel {
                color: #E5E7EB;
                font-weight: 600;
            }

            QSlider::groove:horizontal {
                height: 6px;
                background: #253047;
                border-radius: 3px;
            }
            QSlider::sub-page:horizontal {
                background: #3B82F6;
                border-radius: 3px;
            }
            QSlider::add-page:horizontal {
                background: #253047;
                border-radius: 3px;
            }
            QSlider::handle:horizontal {
                background: #F8FAFC;
                border: 1px solid #64748B;
                width: 16px;
                height: 16px;
                margin: -5px 0;
                border-radius: 8px;
            }
            QSlider::handle:horizontal:hover {
                background: #DBEAFE;
                border-color: #60A5FA;
            }

            #CompactButton {
                min-width: 40px;
                max-width: 54px;
                padding: 6px 8px;
                text-align: center;
            }

            #DangerButton {
                color: #FCA5A5;
            }
            #DangerButton:hover {
                color: #FECACA;
            }

            #HintLabel {
                color: #94A3B8;
                font-size: 12px;
            }

            #MultiSelectLabel {
                color: #94A3B8;
                font-size: 16px;
                font-weight: 600;
            }

            #SelectionActionsBar {
                background: rgba(17, 24, 39, 235);
                border: 1px solid #334155;
                border-radius: 10px;
            }

            #SelectionActionButton {
                text-align: center;
                padding: 6px 12px;
            }

            #CharacterTable {
                background: #0F172A;
                alternate-background-color: #0F172A;
                color: #E5E7EB;
                border: 1px solid #253047;
                border-radius: 8px;
                padding: 4px;
                gridline-color: transparent;
                selection-background-color: #2563EB;
                selection-color: #FFFFFF;
                outline: 0;
            }
            /* Character cells are painted by CharacterMapDelegate so
               populated glyph state is not masked by Qt stylesheet rules. */

            #CanvasScroll {
                background: #0B1220;
                color: #E5E7EB;
                border: 1px solid #253047;
                border-radius: 10px;
            }

            QScrollBar:vertical {
                background: #0F172A;
                width: 12px;
                margin: 0;
            }
            QScrollBar::handle:vertical {
                background: #334155;
                min-height: 24px;
                border-radius: 6px;
            }
            QScrollBar:horizontal {
                background: #0F172A;
                height: 12px;
                margin: 0;
            }
            QScrollBar::handle:horizontal {
                background: #334155;
                min-width: 24px;
                border-radius: 6px;
            }

            QStatusBar {
                background: #111827;
                color: #CBD5E1;
                border-top: 1px solid #253047;
            }

            QMessageBox {
                background: #111827;
                color: #F8FAFC;
            }
            QMessageBox QLabel {
                color: #F8FAFC;
            }
        """)

    def _dialog_dir(self, key):
        saved = self.settings.value(f"dialog_paths/{key}", "", type=str)
        if saved and Path(saved).exists():
            return saved
        return str(Path.home())

    def _remember_dialog_dir(self, key, path):
        if not path:
            return
        p = Path(path)
        directory = p if p.is_dir() else p.parent
        self.settings.setValue(f"dialog_paths/{key}", str(directory))

    def _signmatrix_default_basename(self):
        """Use the currently opened FNT filename exactly as the export base name."""
        if self.current_path:
            return self.current_path.stem

        if self.model and getattr(self.model, "path", None):
            return Path(self.model.path).stem

        return "font"

    def _signmatrix_width_type(self):
        """Determine fixed vs variable width from the actual glyph data.

        Space, non-printing/control characters, and completely empty glyphs are
        ignored. If every remaining meaningful glyph has the same bitmap width,
        the font is considered fixed width; otherwise it is variable width.
        """
        if not self.model:
            return "variable width"

        widths = []

        for code in range(self.model.first, self.model.last + 1):
            ch = chr(code)
            idx = code - self.model.first

            # Ignore space and non-printing/control placeholders.
            if code == 0x20 or not ch.isprintable():
                continue

            width = self.model.width(idx)

            # Ignore glyphs whose bitmap is completely empty.
            has_lit_pixel = False
            for y in range(self.model.height):
                for x in range(width):
                    if self.model.pixel(idx, x, y):
                        has_lit_pixel = True
                        break
                if has_lit_pixel:
                    break

            if not has_lit_pixel:
                continue

            widths.append(width)

        if widths and all(w == widths[0] for w in widths):
            return "fixed width"

        return "variable width"

    def _filename_stroke_weight(self, basename):
        """Return an explicit stroke weight encoded by the filename, if any."""
        name = basename.lower()

        if (
            "quad" in name
            or "quadruple" in name
            or re.search(r"q(?:n)?vw", name)
        ):
            return 4

        if "triple" in name or re.search(r"t(?:n)?vw", name):
            return 3

        if "double" in name or re.search(r"d(?:n)?vw", name):
            return 2

        if "single" in name:
            return 1

        return None

    def _bitmap_stroke_weight(self):
        """Estimate stroke thickness from simple, straight-stem glyphs.

        Preferred glyphs such as I, 1, L and T contain long straight stems.
        For those glyphs we collect short horizontal/vertical runs (1..4 px).
        Long bars are ignored. The most common run length is taken as the
        estimated stroke thickness, provided the samples agree reasonably well.

        Returns 1..4 on a confident estimate, otherwise None.
        """
        if not self.model:
            return None

        preferred = "I1LTEFH"
        samples = []

        for ch in preferred:
            code = ord(ch)
            if not (self.model.first <= code <= self.model.last):
                continue

            idx = code - self.model.first
            width = self.model.width(idx)
            height = self.model.height

            # Skip blank glyphs.
            if not any(
                self.model.pixel(idx, x, y)
                for y in range(height)
                for x in range(width)
            ):
                continue

            glyph_samples = []

            # Horizontal runs measure the thickness of vertical stems.
            for y in range(height):
                x = 0
                while x < width:
                    if not self.model.pixel(idx, x, y):
                        x += 1
                        continue

                    start = x
                    while x < width and self.model.pixel(idx, x, y):
                        x += 1
                    run = x - start

                    # Ignore long crossbars; retain plausible stroke widths.
                    if 1 <= run <= 4:
                        glyph_samples.append(run)

            # Vertical runs measure the thickness of horizontal stems.
            for x in range(width):
                y = 0
                while y < height:
                    if not self.model.pixel(idx, x, y):
                        y += 1
                        continue

                    start = y
                    while y < height and self.model.pixel(idx, x, y):
                        y += 1
                    run = y - start

                    if 1 <= run <= 4:
                        glyph_samples.append(run)

            # Prevent one glyph from overwhelming all the others. Convert each
            # glyph to its own modal stroke estimate first.
            if glyph_samples:
                counts = {w: glyph_samples.count(w) for w in range(1, 5)}
                best = max(counts, key=lambda w: (counts[w], -w))
                total = sum(counts.values())
                confidence = counts[best] / total if total else 0.0

                # A glyph can be somewhat ambiguous due to corners/diagonals,
                # so accept a modest per-glyph majority and combine glyph votes.
                if confidence >= 0.35:
                    samples.append(best)

        if not samples:
            return None

        counts = {w: samples.count(w) for w in range(1, 5)}
        best = max(counts, key=lambda w: (counts[w], -w))
        confidence = counts[best] / len(samples)

        # Require either agreement from at least two characters, or a single
        # usable character when no other preferred glyph exists.
        if len(samples) == 1:
            return best

        if confidence >= 0.60:
            return best

        return None

    def _signmatrix_stroke_weight(self, basename):
        """Choose the best available stroke-weight signal.

        Explicit filename markers (double/triple/quad/single) take priority.
        Otherwise use bitmap analysis of simple glyphs. If that is inconclusive,
        fall back to single stroke, matching the editor's historical behavior.
        """
        explicit = self._filename_stroke_weight(basename)
        if explicit is not None:
            return explicit

        inferred = self._bitmap_stroke_weight()
        if inferred is not None:
            return inferred

        return 1

    def _signmatrix_description(self, basename):
        """Generate an editable default SignMatrix description.

        Stroke weight comes from an explicit filename marker when present;
        otherwise it is estimated from straight-stem glyphs in the bitmap.
        Fixed/variable width is determined from the actual non-empty printable
        glyph widths.
        """
        weight = self._signmatrix_stroke_weight(basename)
        stroke_names = {
            1: "single stroke",
            2: "double stroke",
            3: "triple stroke",
            4: "quad stroke",
        }
        stroke = stroke_names.get(weight, "single stroke")

        width_type = self._signmatrix_width_type()
        return f"{self.model.height} height, {stroke}, {width_type}"

    def _signmatrix_character_order(self):
        """Character order used by the supplied SignMatrix font atlases."""
        return (
            "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
            "abcdefghijklmnopqrstuvwxyz"
            "1234567890"
            " !@#$%^&*()-_=+[{]}\\|;:'\\\",<.>/?`~"
        )

    def _export_character_entries(self):
        """Return visible source glyphs as (source code, label, index) tuples."""
        labels = getattr(self.model, "display_labels", {})
        entries = []
        for index, code in enumerate(range(self.model.first, self.model.last + 1)):
            label = " " if code == 0x20 else labels.get(
                code, display_character_for_code(code)
            )
            has_pixels = any(
                self.model.pixel(index, x, y)
                for y in range(self.model.height)
                for x in range(self.model.width(index))
            )
            if has_pixels or (code == 0x20 and self.model.width(index) > 0):
                entries.append((code, label, index))
        return entries

    def _target_character_code(self, label, target_format):
        if len(label) != 1:
            return None
        if target_format == "luminator":
            return ord(label) if 0x20 <= ord(label) <= 0x7E else None
        if target_format == "hanover":
            try:
                code = label.encode("cp1252")[0]
            except UnicodeEncodeError:
                return None
            return code if code >= HanoverFont.FIRST_CODE else None
        if target_format == "signmatrix":
            # SignMatrix JSON stores Unicode text, and its glyph atlas has no
            # one-byte character-code limit. The Luminator importer has that
            # limit, but it must not restrict a SignMatrix export.
            return ord(label) if label.isprintable() else None
        raise ValueError(f"Unknown target format: {target_format}")

    def _target_conversion_issues(self, target_format):
        unsupported = []
        oversized = []
        collisions = {}
        mapped = {}
        for code, label, index in self._export_character_entries():
            target_code = self._target_character_code(label, target_format)
            if target_code is None:
                unsupported.append((code, label))
                continue
            if target_format == "hanover" and self.model.width(index) > 32:
                oversized.append((code, label, self.model.width(index)))
                continue
            if target_code in mapped:
                collisions.setdefault(target_code, [mapped[target_code]])
                collisions[target_code].append((code, label, index))
            else:
                mapped[target_code] = (code, label, index)
        return unsupported, oversized, collisions

    def _confirm_target_character_support(self, target_format):
        unsupported, oversized, collisions = self._target_conversion_issues(target_format)
        if not unsupported and not oversized and not collisions:
            return True

        target_name = {
            "luminator": "Luminator FNT",
            "hanover": "Hanover FNT",
            "signmatrix": "SignMatrix",
        }[target_format]
        details = []
        if unsupported:
            details.append(
                "Unsupported characters (their glyphs will be omitted):\n"
                + " ".join(f"{label!r} (0x{code:02X})" for code, label in unsupported)
            )
        if collisions:
            details.append(
                "Character mapping collisions (only the first glyph will be kept):\n"
                + " ".join(
                    f"{entries[0][1]!r}" for entries in collisions.values()
                )
            )
        if oversized:
            details.append(
                "Glyphs wider than Hanover's 32-pixel limit cannot be converted:\n"
                + " ".join(f"{label!r} ({width}px)" for _, label, width in oversized)
            )
        reply = QMessageBox.warning(
            self,
            "Character conversion warning",
            f"{target_name} cannot represent all source glyphs.\n\n"
            + "\n\n".join(details)
            + "\n\nContinue and omit the unsupported glyphs?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        return reply == QMessageBox.StandardButton.Yes

    def _convert_model_for_fnt(self, target_format):
        """Build a native FNT model after target character support is confirmed."""
        entries = self._export_character_entries()
        mapped = {}
        for code, label, index in entries:
            target_code = self._target_character_code(label, target_format)
            is_oversized = (
                target_format == "hanover" and self.model.width(index) > 32
            )
            if target_code is not None and not is_oversized and target_code not in mapped:
                mapped[target_code] = index

        if target_format == "luminator":
            target = LuminatorFont.create_blank(
                "CONVERTED", self.model.height, self.model.spacing, 0x20, 0x7E, 1
            )
        elif target_format == "hanover":
            target = HanoverFont.__new__(HanoverFont)
            target.path = None
            target.version = 2
            target.height = self.model.height
            target.max_width = 1
            target.bitmap_columns = 1
            target.has_width_footer = False
            target.character_set = 0
            target.spacing = self.model.spacing
            # HELEN's legacy text reader expects DOS records when importing
            # newly generated fonts; native Hanover files use CRLF as well.
            target.newline = "\r\n"
            target.first = HanoverFont.FIRST_CODE
            target.last = HanoverFont.LAST_CODE
            target.count = HanoverFont.GLYPH_COUNT
            target.bytes_per_col = (target.height + 7) // 8
            target.glyphs = [bytearray() for _ in range(target.count)]
            target.advance_widths = [0] * target.count
            target.display_labels = hanover_display_labels()
        else:
            raise ValueError(f"Unknown FNT target format: {target_format}")

        for target_code, source_index in mapped.items():
            if not (target.first <= target_code <= target.last):
                continue
            width = self.model.width(source_index)
            target_index = target_code - target.first
            target.glyphs[target_index] = bytearray(width * target.bytes_per_col)
            for y in range(target.height):
                for x in range(width):
                    if self.model.pixel(source_index, x, y):
                        target.set_pixel(target_index, x, y, True)
            if target_format == "hanover":
                target.max_width = max(target.max_width, width)
                target.bitmap_columns = target.max_width
                target.advance_widths[target_index] = width
        return target

    def _confirm_luminator_omissions(self):
        omitted = self.model.deployment_warnings()
        if not omitted:
            return True
        characters = " ".join(omitted)
        reply = QMessageBox.warning(
            self,
            "Accented Characters Omitted",
            "Luminator FNT supports the printable ASCII character range only. "
            f"These characters will be omitted and their glyph slots left blank:\n\n"
            f"{characters}\n\nContinue saving?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        return reply == QMessageBox.StandardButton.Yes

    def export_for_signmatrix(self, json_path=None):
        if not self.model:
            QMessageBox.information(
                self,
                "No font open",
                "Open a .fnt file before exporting for SignMatrix."
            )
            return

        if not self._confirm_target_character_support("signmatrix"):
            return

        if json_path is None:
            default_basename = self._signmatrix_default_basename()
            default_path = str(
                Path(self._dialog_dir("signmatrix_export"))
                / f"{default_basename}.json"
            )
            chosen_path, _ = QFileDialog.getSaveFileName(
                self,
                "Save font as",
                default_path,
                "SignMatrix JSON (*.json);;All files (*.*)"
            )
            if not chosen_path:
                return
            json_path = chosen_path

        self._remember_dialog_dir("signmatrix_export", json_path)
        json_path = Path(json_path)
        if json_path.suffix.lower() != ".json":
            json_path = json_path.with_suffix(".json")

        basename = json_path.stem
        png_path = json_path.with_name(f"{basename}.png")

        default_desc = self._signmatrix_description(basename)
        description, ok = QInputDialog.getText(
            self,
            "SignMatrix description",
            "Description:",
            text=default_desc,
        )
        if not ok:
            return

        # Keep the generated default if the field is left completely blank.
        description = description.strip() or default_desc

        if json_path.exists() or png_path.exists():
            answer = QMessageBox.question(
                self,
                "Replace existing SignMatrix export?",
                f"{json_path.name} or {png_path.name} already exists in this folder.\n\n"
                "Replace the existing export files?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return

        try:
            result = self._write_signmatrix_export(
                json_path, png_path, basename, description
            )
        except Exception as e:
            QMessageBox.critical(
                self,
                "SignMatrix export failed",
                str(e)
            )
            self.statusBar().showMessage("SignMatrix export failed", 5000)
            return

        QMessageBox.information(
            self,
            "SignMatrix export complete",
            f"Exported {result['glyph_count']} glyphs:\n\n"
            f"{json_path}\n{png_path}\n\n"
            f"Sprite sheet: {result['image_width']} × {result['image_height']} pixels"
        )
        self.statusBar().showMessage(
            f"Exported SignMatrix font: {basename}",
            5000
        )

    def _write_signmatrix_export(self, json_path, png_path, basename, description):
        """Write SignMatrix JSON metadata plus a transparent white-pixel PNG atlas.

        The supplied examples use:
          - a global font height
          - a PNG filename in `img`
          - a descriptive `desc`
          - global `advance` equal to Luminator letter spacing
          - glyph metadata containing char/x/y/w
          - letters on row 0
          - digits, space and punctuation on row 1
          - one source font pixel = one PNG pixel
          - white opaque lit pixels on a transparent background
          - a 355-pixel-wide atlas in the supplied examples

        The atlas is kept at least 355 pixels wide, but expands if a wider font
        requires more room so glyphs are never clipped.
        """
        height = int(self.model.height)
        advance = int(self.model.spacing)

        available = {
            label: index
            for _code, label, index in self._export_character_entries()
            if self._target_character_code(label, "signmatrix") is not None
        }

        # Filter the canonical SignMatrix ordering to characters actually
        # present in the open FNT. This avoids inventing glyphs the source font
        # does not contain.
        chars = []
        seen = set()
        for ch in self._signmatrix_character_order():
            if ch in available and ch not in seen:
                chars.append(ch)
                seen.add(ch)

        # Preserve any extra printable characters from an unusual FNT after the
        # canonical set, rather than silently dropping them.
        for code in range(self.model.first, self.model.last + 1):
            ch = " " if code == 0x20 else getattr(
                self.model, "display_labels", {}
            ).get(code, chr(code))
            if ch in available and ch not in seen and ch.isprintable():
                chars.append(ch)
                seen.add(ch)

        letter_chars = [
            ch for ch in chars
            if ("A" <= ch <= "Z") or ("a" <= ch <= "z")
        ]
        other_chars = [ch for ch in chars if ch not in set(letter_chars)]

        placements = []
        row_widths = []

        for row_index, row_chars in enumerate((letter_chars, other_chars)):
            x = 0
            y = row_index * height

            for ch in row_chars:
                idx = available[ch]
                width = int(self.model.width(idx))
                glyph = {
                    "char": ch,
                    "x": x,
                    "y": y,
                    "w": width,
                }
                placements.append((glyph, idx))
                x += width + advance

            # Do not count the trailing inter-character spacing as content.
            used = max(0, x - advance) if row_chars else 0
            row_widths.append(used)

        image_width = max(355, max(row_widths, default=0))
        image_height = height * 2

        image = QImage(
            image_width,
            image_height,
            QImage.Format.Format_RGBA8888
        )
        image.fill(QColor(0, 0, 0, 0))

        white = QColor(255, 255, 255, 255)
        glyph_json = []

        for glyph, idx in placements:
            gx = glyph["x"]
            gy = glyph["y"]
            width = glyph["w"]

            for y in range(height):
                for x in range(width):
                    if self.model.pixel(idx, x, y):
                        image.setPixelColor(gx + x, gy + y, white)

            glyph_json.append(glyph)

        if not image.save(str(png_path), "PNG"):
            raise RuntimeError(f"Could not save PNG file:\n{png_path}")

        metadata = {
            "height": height,
            "img": png_path.name,
            "desc": description,
            "advance": advance,
            "glyphs": glyph_json,
        }

        json_path.write_text(
            json.dumps(metadata, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8"
        )

        return {
            "glyph_count": len(glyph_json),
            "image_width": image_width,
            "image_height": image_height,
        }

    def extract_fnts_from_ips(self):
        if os.name != "nt":
            QMessageBox.warning(
                self,
                "Windows required",
                "IPS font extraction currently uses Microsoft Access/ADO components "
                "and is supported on Windows."
            )
            return

        ips_path, _ = QFileDialog.getOpenFileName(
            self,
            "Choose Luminator IPS file",
            self._dialog_dir("ips_open"),
            "Luminator IPS (*.ips);;All files (*.*)"
        )
        if not ips_path:
            return
        self._remember_dialog_dir("ips_open", ips_path)

        output_dir = QFileDialog.getExistingDirectory(
            self,
            "Choose folder for extracted FNT files",
            self._dialog_dir("ips_extract")
        )
        if not output_dir:
            return
        self._remember_dialog_dir("ips_extract", output_dir)

        self.statusBar().showMessage("Extracting fonts from IPS...")
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            result = self._run_ips_font_extractor(Path(ips_path), Path(output_dir))
        except Exception as e:
            QMessageBox.critical(self, "IPS extraction failed", str(e))
            self.statusBar().showMessage("IPS extraction failed", 5000)
            return
        finally:
            QApplication.restoreOverrideCursor()

        count = result.get("count", 0)
        files = result.get("files", [])
        provider = result.get("provider", "")

        if count:
            preview = "\n".join(files[:12])
            if len(files) > 12:
                preview += f"\n... and {len(files) - 12} more"

            provider_text = f"\n\nDatabase provider: {provider}" if provider else ""
            QMessageBox.information(
                self,
                "Fonts extracted",
                f"Extracted {count} FNT file{'s' if count != 1 else ''} to:\n"
                f"{output_dir}{provider_text}\n\n{preview}"
            )
            self.statusBar().showMessage(
                f"Extracted {count} FNT file{'s' if count != 1 else ''}",
                5000
            )
        else:
            QMessageBox.warning(
                self,
                "No fonts extracted",
                "The IPS file opened successfully, but no embedded font records "
                "were extracted from the Fonts table."
            )
            self.statusBar().showMessage("No fonts found in IPS", 5000)

    def _run_ips_font_extractor(self, ips_path, output_dir):
        """Extract embedded FNT blobs from a Luminator IPS Access database."""
        output_dir.mkdir(parents=True, exist_ok=True)

        ps_script = r'''
param(
    [Parameter(Mandatory=$true)][string]$IpsPath,
    [Parameter(Mandatory=$true)][string]$OutputDir
)

$ErrorActionPreference = "Stop"

function Open-IpsConnection {
    param([string]$Path)

    $providers = @(
        "Microsoft.ACE.OLEDB.16.0",
        "Microsoft.ACE.OLEDB.12.0",
        "Microsoft.Jet.OLEDB.4.0"
    )

    $errors = @()

    foreach ($provider in $providers) {
        $conn = $null
        try {
            $conn = New-Object -ComObject ADODB.Connection
            $conn.ConnectionString = "Provider=$provider;Data Source=$Path;Persist Security Info=False;"
            $conn.Open()

            return [PSCustomObject]@{
                Connection = $conn
                Provider   = $provider
            }
        }
        catch {
            $errors += "$provider : $($_.Exception.Message)"
            try {
                if ($conn -and $conn.State -ne 0) { $conn.Close() }
            } catch {}
        }
    }

    throw ("Could not open the IPS database with an installed Access/Jet provider.`n" +
           ($errors -join "`n"))
}

function Safe-FileName {
    param([string]$Name, [int]$Index)

    if ([string]::IsNullOrWhiteSpace($Name)) {
        $Name = ("font_{0:D3}.fnt" -f $Index)
    }

    $Name = [IO.Path]::GetFileName($Name)

    foreach ($bad in [IO.Path]::GetInvalidFileNameChars()) {
        $Name = $Name.Replace([string]$bad, "_")
    }

    if (-not $Name.ToLowerInvariant().EndsWith(".fnt")) {
        $Name += ".fnt"
    }

    return $Name
}

if (-not (Test-Path -LiteralPath $OutputDir)) {
    New-Item -ItemType Directory -Path $OutputDir -Force | Out-Null
}

$opened = Open-IpsConnection -Path $IpsPath
$conn = $opened.Connection
$provider = $opened.Provider
$recordset = $null

try {
    $recordset = New-Object -ComObject ADODB.Recordset
    $recordset.Open("SELECT FontFile, Font FROM Fonts", $conn, 0, 1)

    $index = 0
    $written = @()

    while (-not $recordset.EOF) {
        $index++

        $rawName = $recordset.Fields.Item("FontFile").Value
        $blob = $recordset.Fields.Item("Font").Value

        if ($null -ne $blob -and $blob -isnot [System.DBNull]) {
            $name = Safe-FileName -Name ([string]$rawName) -Index $index
            $dest = Join-Path $OutputDir $name

            if (Test-Path -LiteralPath $dest) {
                $stem = [IO.Path]::GetFileNameWithoutExtension($name)
                $ext = [IO.Path]::GetExtension($name)
                $n = 2
                do {
                    $candidate = "{0}_{1}{2}" -f $stem, $n, $ext
                    $dest = Join-Path $OutputDir $candidate
                    $n++
                } while (Test-Path -LiteralPath $dest)
            }

            $bytes = [byte[]]$blob
            [IO.File]::WriteAllBytes($dest, $bytes)
            $written += [IO.Path]::GetFileName($dest)
        }

        $recordset.MoveNext()
    }

    Write-Output ("__PROVIDER__=" + $provider)
    Write-Output ("__COUNT__=" + $written.Count)
    foreach ($file in $written) {
        Write-Output ("__FILE__=" + $file)
    }
}
finally {
    try {
        if ($recordset -and $recordset.State -ne 0) { $recordset.Close() }
    } catch {}

    try {
        if ($conn -and $conn.State -ne 0) { $conn.Close() }
    } catch {}
}
'''

        temp_path = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                suffix=".ps1",
                encoding="utf-8-sig",
                delete=False
            ) as tf:
                tf.write(ps_script)
                temp_path = Path(tf.name)

            system_root = Path(os.environ.get("SystemRoot", r"C:\Windows"))
            candidates = [
                system_root / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe",
                system_root / "SysWOW64" / "WindowsPowerShell" / "v1.0" / "powershell.exe",
                Path("powershell.exe"),
            ]

            errors = []
            seen = set()

            for candidate in candidates:
                powershell = str(candidate)
                if powershell in seen:
                    continue
                seen.add(powershell)

                if powershell != "powershell.exe" and not Path(powershell).exists():
                    continue

                cmd = [
                    powershell,
                    "-NoProfile",
                    "-ExecutionPolicy", "Bypass",
                    "-File", str(temp_path),
                    "-IpsPath", str(ips_path),
                    "-OutputDir", str(output_dir),
                ]

                creationflags = 0
                if hasattr(subprocess, "CREATE_NO_WINDOW"):
                    creationflags = subprocess.CREATE_NO_WINDOW

                try:
                    proc = subprocess.run(
                        cmd,
                        capture_output=True,
                        text=True,
                        encoding="utf-8",
                        errors="replace",
                        creationflags=creationflags,
                        timeout=120,
                    )
                except Exception as e:
                    errors.append(f"{powershell}: {e}")
                    continue

                if proc.returncode != 0:
                    detail = (proc.stderr or proc.stdout or "").strip()
                    errors.append(
                        f"{powershell}:\n{detail or 'PowerShell exited with an error.'}"
                    )
                    continue

                provider = ""
                count = 0
                files = []

                for line in proc.stdout.splitlines():
                    if line.startswith("__PROVIDER__="):
                        provider = line.split("=", 1)[1].strip()
                    elif line.startswith("__COUNT__="):
                        try:
                            count = int(line.split("=", 1)[1].strip())
                        except ValueError:
                            pass
                    elif line.startswith("__FILE__="):
                        files.append(line.split("=", 1)[1].strip())

                return {
                    "provider": provider,
                    "count": count,
                    "files": files,
                }

            raise RuntimeError(
                "Unable to extract fonts from the IPS file.\n\n"
                "The application tried both 64-bit and 32-bit Windows PowerShell "
                "and the common Microsoft ACE/Jet database providers.\n\n"
                + "\n\n".join(errors[-3:])
            )
        finally:
            if temp_path is not None:
                try:
                    temp_path.unlink(missing_ok=True)
                except Exception:
                    pass

    def _glyph_has_pixels(self, code):
        """True when the glyph contains at least one visible/on pixel."""
        if not self.model or not (self.model.first <= code <= self.model.last):
            return False

        idx = code - self.model.first
        width = self.model.width(idx)
        for y in range(self.model.height):
            for x in range(width):
                if self.model.pixel(idx, x, y):
                    return True
        return False

    def _character_item_should_be_green(self, code):
        # Space is deliberately highlighted even when its bitmap is blank,
        # because its blank columns still carry meaningful character width.
        return code == 0x20 or self._glyph_has_pixels(code)

    def _apply_character_item_style(self, item, code):
        if not item:
            return
        populated = self._character_item_should_be_green(code)
        item.setData(CharacterMapDelegate.POPULATED_ROLE, populated)

    def _update_character_item_style(self, code):
        """Refresh only one character cell after editing its bitmap."""
        if not self.model or not (self.model.first <= code <= self.model.last):
            return
        idx = code - self.model.first
        row, col = divmod(idx, self._char_columns)
        item = self.char_table.item(row, col)
        self._apply_character_item_style(item, code)

    def _calculate_character_columns(self):
        """Choose a column count that can never require horizontal scrolling."""
        if not hasattr(self, "char_table"):
            return 1

        # Reserve room for the vertical scrollbar even before it appears.
        viewport_w = max(1, self.char_table.viewport().width())
        scrollbar_w = max(0, self.char_table.verticalScrollBar().sizeHint().width())
        available = max(self._char_cell_min_width, viewport_w - scrollbar_w - 2)
        return max(1, available // self._char_cell_min_width)

    def _rebuild_character_table(self, select_code=None):
        if not self.model:
            self.char_table.clearContents()
            self.char_table.setRowCount(0)
            return

        # Preserve the selected character while the table changes shape.
        if select_code is None:
            selected = self.char_table.currentItem()
            if selected:
                select_code = selected.data(Qt.ItemDataRole.UserRole)

        columns = self._calculate_character_columns()
        self._char_columns = columns
        self.char_table.setColumnCount(columns)

        count = self.model.last - self.model.first + 1
        rows = (count + columns - 1) // columns
        self.char_table.clearContents()
        self.char_table.setRowCount(rows)

        for i, code in enumerate(range(self.model.first, self.model.last + 1)):
            row, col = divmod(i, columns)
            ch = getattr(self.model, "display_labels", {}).get(
                code, display_character_for_code(code)
            )
            display = ch
            if code == 0x20:
                display = ""
            elif (
                code < 0x20
                and code not in getattr(self.model, "display_labels", {})
            ) or ch.isspace():
                display = "·"

            item = QTableWidgetItem(display)
            item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            item.setData(Qt.ItemDataRole.UserRole, code)
            item.setToolTip(f"0x{code:02X}  {repr(ch)}")
            self._apply_character_item_style(item, code)
            self.char_table.setItem(row, col, item)

        for r in range(rows):
            self.char_table.setRowHeight(r, 28)

        if select_code is not None and self.model.first <= select_code <= self.model.last:
            idx = select_code - self.model.first
            row, col = divmod(idx, columns)
            if self.char_table.item(row, col):
                self.char_table.setCurrentCell(row, col)

    def eventFilter(self, obj, event):
        if (
            hasattr(self, "char_table")
            and obj is self.char_table.viewport()
            and event.type() == QEvent.Type.Resize
        ):
            if self.model:
                new_columns = self._calculate_character_columns()
                if new_columns != getattr(self, "_char_columns", 1):
                    # Defer rebuilding until Qt finishes the current resize pass.
                    QTimer.singleShot(0, self._rebuild_character_table)

        if (
            hasattr(self, "scroll")
            and obj is self.scroll.viewport()
            and event.type() == QEvent.Type.MouseButtonPress
        ):
            # A press reaching the viewport (rather than the canvas widget)
            # landed on the blank margin around the glyph, not on a pixel.
            if self.canvas.selection is not None:
                self.canvas.clear_selection()

        if (
            hasattr(self, "scroll")
            and event.type() == QEvent.Type.Resize
            and obj in (self.scroll, self.scroll.viewport(), self.canvas)
        ):
            # Defer until Qt finishes laying out the resize/maximize/snap
            # pass, otherwise mapTo() can read stale intermediate geometry.
            QTimer.singleShot(0, self._position_selection_actions_bar)

        return super().eventFilter(obj, event)

    def _position_selection_actions_bar(self):
        """Keep the floating action bar centered above the canvas, clamped to
        the visible viewport so it never drifts when the canvas is larger
        than the viewport or the window is resized.
        """
        if not hasattr(self, "selection_actions_bar"):
            return
        bar = self.selection_actions_bar
        bar.adjustSize()

        viewport = self.scroll.viewport()
        viewport_origin = viewport.mapTo(self.scroll, QPoint(0, 0))
        canvas_origin = self.canvas.mapTo(self.scroll, QPoint(0, 0))

        canvas_center_x = canvas_origin.x() + self.canvas.width() // 2
        x = canvas_center_x - bar.width() // 2

        min_x = viewport_origin.x()
        max_x = max(min_x, viewport_origin.x() + viewport.width() - bar.width())
        x = max(min_x, min(x, max_x))

        bar.move(x, viewport_origin.y() + 10)
        bar.raise_()

    def _update_selection_actions_bar(self):
        if not hasattr(self, "selection_actions_bar"):
            return
        has_model = bool(self.model)
        self.selection_actions_bar.setVisible(has_model)
        if has_model:
            self._position_selection_actions_bar()

        selection_active = has_model and self.canvas.selection is not None
        self.set_lit_btn.setEnabled(selection_active)
        self.set_unlit_btn.setEnabled(selection_active)
        self.copy_btn.setEnabled(has_model)
        self.paste_btn.setEnabled(has_model and self.clipboard is not None)
        self.mirror_h_btn.setEnabled(has_model)
        self.mirror_v_btn.setEnabled(has_model)

    def set_selection_pixels(self, value):
        """Set every pixel in the active selection box to lit/unlit."""
        if not self.model or self.canvas.selection is None:
            return

        left, top, right, bottom = self.canvas.selection
        idx = self.canvas.glyph_index

        self._begin_history_action()
        for y in range(top, bottom + 1):
            for x in range(left, right + 1):
                self.model.set_pixel(idx, x, y, value)
        self._commit_history_action()

        self.canvas.update()
        code = self.model.first + idx
        self._update_character_item_style(code)
        self.mark_dirty()

    def copy_pixels(self):
        """Copy the selection, or the whole glyph when nothing is selected."""
        if not self.model:
            return

        idx = self.canvas.glyph_index
        if self.canvas.selection is not None:
            left, top, right, bottom = self.canvas.selection
        else:
            left, top = 0, 0
            right = self.model.width(idx) - 1
            bottom = self.model.height - 1

        rows = [
            [self.model.pixel(idx, x, y) for x in range(left, right + 1)]
            for y in range(top, bottom + 1)
        ]
        self.clipboard = {
            "width": right - left + 1,
            "height": bottom - top + 1,
            "rows": rows,
        }
        self._update_selection_actions_bar()
        self.statusBar().showMessage(
            f"Copied {self.clipboard['width']}x{self.clipboard['height']} pixels", 2500
        )

    def mirror_pixels(self, axis):
        """Flip the selection, or the whole glyph if nothing is selected."""
        if not self.model:
            return

        idx = self.canvas.glyph_index
        if self.canvas.selection is not None:
            left, top, right, bottom = self.canvas.selection
        else:
            left, top = 0, 0
            right = self.model.width(idx) - 1
            bottom = self.model.height - 1

        rows = [
            [self.model.pixel(idx, x, y) for x in range(left, right + 1)]
            for y in range(top, bottom + 1)
        ]
        flipped = [row[::-1] for row in rows] if axis == "horizontal" else rows[::-1]

        self._begin_history_action()
        self._paste_rows(
            idx, flipped, right - left + 1, bottom - top + 1, origin_x=left, origin_y=top
        )
        self._commit_history_action()

        self.canvas.update()
        code = self.model.first + idx
        self._update_character_item_style(code)
        self.mark_dirty()
        label = "left-right" if axis == "horizontal" else "top-bottom"
        self.statusBar().showMessage(f"Mirrored {label}", 2500)

    def _paste_rows(self, idx, rows, max_w, max_h, origin_x=0, origin_y=0):
        """Blit clipboard rows at (origin_x, origin_y), clipped to max_w/max_h."""
        for sy, row in enumerate(rows):
            if sy >= max_h:
                break
            for sx, value in enumerate(row):
                if sx >= max_w:
                    break
                self.model.set_pixel(idx, origin_x + sx, origin_y + sy, value)

    def paste_pixels(self):
        """Paste clipboard pixels into the selection, or the top-left corner."""
        if not self.model or not self.clipboard:
            return

        idx = self.canvas.glyph_index
        clip_w = self.clipboard["width"]
        clip_h = self.clipboard["height"]
        rows = self.clipboard["rows"]

        # With an active selection, the paste is always clipped to that box
        # and starts at its top-left corner; the glyph itself never resizes.
        if self.canvas.selection is not None:
            left, top, right, bottom = self.canvas.selection
            sel_w = right - left + 1
            sel_h = bottom - top + 1

            self._begin_history_action()
            self._paste_rows(idx, rows, sel_w, sel_h, origin_x=left, origin_y=top)
            self._commit_history_action()

            self.canvas.update()
            code = self.model.first + idx
            self._update_character_item_style(code)
            self.mark_dirty()
            self.statusBar().showMessage("Pasted into selection", 2500)
            return

        width = self.model.width(idx)
        height = self.model.height

        if clip_w <= width and clip_h <= height:
            self._begin_history_action()
            self._paste_rows(idx, rows, width, height)
            self._commit_history_action()

            self.canvas.update()
            code = self.model.first + idx
            self._update_character_item_style(code)
            self.mark_dirty()
            self.statusBar().showMessage("Pasted", 2500)
            return

        box = QMessageBox(self)
        box.setWindowTitle("Paste exceeds glyph size")
        box.setText(
            f"The copied pixels are {clip_w}x{clip_h}, but this glyph is only "
            f"{width}x{height}.\n\nWiden the glyph to fit the paste, or paste "
            "into the existing area (pixels outside the glyph are dropped)?"
        )
        widen_btn = box.addButton("Widen && Paste", QMessageBox.ButtonRole.AcceptRole)
        clip_btn = box.addButton("Paste As-Is", QMessageBox.ButtonRole.DestructiveRole)
        box.addButton(QMessageBox.StandardButton.Cancel)
        box.setDefaultButton(widen_btn)
        box.exec()
        clicked = box.clickedButton()

        if clicked is widen_btn:
            self._begin_history_action()
            new_height = max(height, clip_h)
            if new_height != height:
                self.model.resize_height(new_height)
            new_width = max(width, clip_w)
            for _ in range(new_width - width):
                self.model.add_column(idx, "right")
            self._paste_rows(idx, rows, new_width, new_height)
            self._commit_history_action()

            self.canvas.update_geometry()
            self.canvas.update()
            code = self.model.first + idx
            self._update_character_item_style(code)
            self.refresh_metrics()
            self.mark_dirty()
            if not self.manual_scale_override:
                QTimer.singleShot(0, self.auto_fit_canvas)
            self.statusBar().showMessage("Widened glyph and pasted", 2500)
        elif clicked is clip_btn:
            self._begin_history_action()
            self._paste_rows(idx, rows, width, height)
            self._commit_history_action()

            self.canvas.update()
            code = self.model.first + idx
            self._update_character_item_style(code)
            self.mark_dirty()
            self.statusBar().showMessage("Pasted (clipped to glyph size)", 2500)

    def _load_model_into_editor(self, display_name):
        """Populate all editor controls from self.model."""
        self.dirty = False
        self.filename_label.setText(display_name)
        self.save_action.setEnabled(True)
        self.save_as_action.setEnabled(True)

        self._rebuild_character_table()

        self.spacing_spin.blockSignals(True)
        self.spacing_spin.setValue(self.model.spacing)
        self.spacing_spin.blockSignals(False)

        self.undo_stack.clear()
        self.redo_stack.clear()
        self._pending_history_snapshot = None
        self.update_undo_redo_buttons()

        self.manual_scale_override = False
        self.canvas.set_scale_factor(1.0)
        self.scale_slider.blockSignals(True)
        self.scale_slider.setValue(100)
        self.scale_slider.blockSignals(False)
        self.scale_value_label.setText("100%")

        self.canvas.set_font_model(self.model)
        if self.char_table.item(0, 0):
            self.char_table.setCurrentCell(0, 0)
            self._select_character_code(self.model.first)

        self.refresh_metrics()
        self._update_selection_actions_bar()
        QTimer.singleShot(0, self.auto_fit_canvas)

    def create_new_font(self):
        """Create a completely blank FNT and open it directly in the editor."""
        dialog = QDialog(self)
        dialog.setWindowTitle("Create New Luminator Font")
        dialog.setModal(True)

        layout = QVBoxLayout(dialog)
        form = QFormLayout()

        name_edit = QLineEdit("NewFont")
        name_edit.setMaxLength(64)
        form.addRow("Font name", name_edit)

        height_spin = QSpinBox()
        height_spin.setRange(1, 32)
        height_spin.setValue(8)
        height_spin.setSuffix(" px")
        form.addRow("Height", height_spin)

        spacing_spin = QSpinBox()
        spacing_spin.setRange(0, 9)
        spacing_spin.setValue(1)
        spacing_spin.setSuffix(" px")
        form.addRow("Letter spacing", spacing_spin)

        first_edit = QLineEdit("0x20")
        first_edit.setToolTip("First character: a single character or byte code such as 0x20")
        form.addRow("First character", first_edit)

        last_edit = QLineEdit("0x7E")
        last_edit.setToolTip("Last character: a single character or byte code such as 0x7E")
        form.addRow("Last character", last_edit)

        width_spin = QSpinBox()
        width_spin.setRange(1, 64)
        width_spin.setValue(5)
        width_spin.setSuffix(" px")
        width_spin.setToolTip(
            "Every new glyph starts blank at this width; each glyph can then "
            "be resized independently with Add/Remove Column."
        )
        form.addRow("Initial glyph width", width_spin)

        layout.addLayout(form)

        note = QLabel(
            "The FNT format uses one continuous character range. "
            "All characters from First through Last will be created as blank glyphs."
        )
        note.setWordWrap(True)
        note.setObjectName("MutedLabel")
        layout.addWidget(note)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok |
            QDialogButtonBox.StandardButton.Cancel
        )
        ok_button = buttons.button(QDialogButtonBox.StandardButton.Ok)
        if ok_button:
            ok_button.setText("Create Font")
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)

        if dialog.exec() != QDialog.DialogCode.Accepted:
            return

        def parse_code(text, field_name):
            value = text.strip()
            if not value:
                raise ValueError(f"{field_name} cannot be blank.")
            if value.lower().startswith("0x"):
                code = int(value, 16)
            elif len(value) == 1:
                code = ord(value)
            else:
                # Also accept plain decimal byte values.
                code = int(value, 10)
            if not 0 <= code <= 255:
                raise ValueError(f"{field_name} must be between 0x00 and 0xFF.")
            return code

        try:
            first = parse_code(first_edit.text(), "First character")
            last = parse_code(last_edit.text(), "Last character")
            if first > last:
                raise ValueError("First character must not come after Last character.")

            name = name_edit.text().strip() or "NewFont"
            model = LuminatorFont.create_blank(
                name=name,
                height=height_spin.value(),
                spacing=spacing_spin.value(),
                first=first,
                last=last,
                glyph_width=width_spin.value(),
            )
        except Exception as e:
            QMessageBox.warning(self, "Cannot create font", str(e))
            return

        self.model = model
        safe_name = "".join(
            c if c.isalnum() or c in "-_ " else "_" for c in name
        ).strip() or "NewFont"
        self.current_path = Path(safe_name).with_suffix(".fnt")
        self._imported_from_signmatrix = False
        self._new_from_scratch = True

        self._load_model_into_editor(f"{safe_name}.fnt (new)")
        self.statusBar().showMessage(
            f"Created blank {model.height}px font with {model.count} glyphs; "
            "use Save As to write the FNT",
            6000
        )

    def import_from_signmatrix(self, json_path=None):
        if json_path is None:
            json_path, _ = QFileDialog.getOpenFileName(
                self,
                "Open font",
                self._dialog_dir("signmatrix_import"),
                "SignMatrix JSON (*.json);;All files (*.*)"
            )
            if not json_path:
                return

        self._remember_dialog_dir("signmatrix_import", json_path)

        try:
            model = LuminatorFont.from_signmatrix(json_path)
        except Exception as e:
            QMessageBox.critical(
                self,
                "Cannot import SignMatrix font",
                str(e)
            )
            return

        self.model = model

        # Give Save As a sensible FNT filename without pretending the FNT
        # already exists. The imported JSON stem becomes the FNT stem.
        self.current_path = Path(json_path).with_suffix(".fnt")
        self._imported_from_signmatrix = True
        self._new_from_scratch = False

        self._load_model_into_editor(
            f"{Path(json_path).name} (SignMatrix import)"
        )
        self.statusBar().showMessage(
            f"Imported {Path(json_path).name}; use Save As to create an FNT",
            5000
        )

    def open_file(self, format_hint=None):
        filters = {
            "luminator": "Luminator (.FNT) (*.fnt);;All files (*.*)",
            "hanover": "Hanover (.FNT) (*.fnt);;All files (*.*)",
            "bbm": "Axion (.BBM) (*.BBM *.bbm);;All files (*.*)",
            "signmatrix": "SignMatrix (.JSON+.PNG) (*.json);;All files (*.*)",
        }
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Open font",
            self._dialog_dir("font_open"),
            filters.get(format_hint, "Supported fonts (*.fnt *.BBM *.bbm *.json);;All files (*.*)")
        )
        if not path:
            return
        self._remember_dialog_dir("font_open", path)

        source_path = Path(path)
        if source_path.suffix.lower() == ".json":
            self.import_from_signmatrix(path)
            return

        try:
            is_bbm = source_path.suffix.lower() == ".bbm"
            custom_order = False
            if is_bbm and source_path.stem.upper().endswith("-A"):
                reply = QMessageBox.question(
                    self,
                    "Custom BBM Character Order",
                    "This filename ends in -A. Import using the custom Axion "
                    "character order?",
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                    QMessageBox.StandardButton.Yes,
                )
                custom_order = reply == QMessageBox.StandardButton.Yes
            self.model = (
                LuminatorFont.from_bbm(path, custom_order=custom_order)
                if is_bbm else HanoverFont(path)
                if format_hint == "hanover" else LuminatorFont(path)
            )
        except Exception as e:
            QMessageBox.critical(self, "Cannot open font", str(e))
            return

        self.current_path = source_path.with_suffix(".fnt") if is_bbm else source_path
        self._imported_from_signmatrix = is_bbm
        self._new_from_scratch = False
        if is_bbm:
            label = f"{source_path.name} (BBM import)"
        elif format_hint == "hanover":
            label = f"{self.current_path.name} (Hanover)"
        else:
            label = f"{self.current_path.name} (Luminator)"
        self._load_model_into_editor(label)
        message = (
            f"Imported {source_path.name}; use Save As to create an FNT"
            if is_bbm else f"Opened {self.current_path.name}"
        )
        self.statusBar().showMessage(message, 5000 if is_bbm else 0)

    def _batch_file_filter(self, source_format):
        return {
            "luminator": "Luminator (.FNT) (*.fnt)",
            "hanover": "Hanover (.FNT) (*.fnt)",
            "bbm": "Axion (.BBM) (*.BBM *.bbm)",
            "signmatrix": "SignMatrix (.JSON+.PNG) (*.json)",
        }[source_format] + ";;All files (*.*)"

    def _load_batch_source(self, path, source_format):
        if source_format == "luminator":
            return LuminatorFont(path)
        if source_format == "hanover":
            return HanoverFont(path)
        if source_format == "bbm":
            return LuminatorFont.from_bbm(
                path, custom_order=Path(path).stem.upper().endswith("-A")
            )
        if source_format == "signmatrix":
            return LuminatorFont.from_signmatrix(path)
        raise ValueError(f"Unknown source format: {source_format}")

    def _batch_output_path(self, output_dir, source_path, target_format, used_paths):
        extension = ".json" if target_format == "signmatrix" else ".fnt"
        candidate = output_dir / f"{source_path.stem}{extension}"
        sequence = 2
        while candidate in used_paths or candidate.exists():
            candidate = output_dir / f"{source_path.stem}_{sequence}{extension}"
            sequence += 1
        used_paths.add(candidate)
        return candidate

    def mass_convert_fonts(self):
        """Convert a selected group of font files without changing the open font."""
        dialog = QDialog(self)
        dialog.setWindowTitle("Mass Convert Fonts")
        dialog.setModal(True)
        layout = QVBoxLayout(dialog)
        form = QFormLayout()

        source_format = QComboBox()
        target_format = QComboBox()
        for label, value in self.SOURCE_FORMATS:
            source_format.addItem(label, value)
        for label, value in self.TARGET_FORMATS:
            target_format.addItem(label, value)
        form.addRow("Source format", source_format)
        form.addRow("Target format", target_format)

        selected_paths = []
        source_files_label = QLabel("No files selected")
        source_files_label.setWordWrap(True)
        choose_files = QPushButton("Choose Files")

        def choose_source_files():
            paths, _ = QFileDialog.getOpenFileNames(
                dialog,
                "Choose source fonts",
                self._dialog_dir("batch_source"),
                self._batch_file_filter(source_format.currentData()),
            )
            if not paths:
                return
            selected_paths[:] = [Path(path) for path in paths]
            self._remember_dialog_dir("batch_source", paths[0])
            source_files_label.setText(f"{len(selected_paths)} file(s) selected")

        choose_files.clicked.connect(choose_source_files)
        source_row = QHBoxLayout()
        source_row.addWidget(choose_files)
        source_row.addWidget(source_files_label, 1)
        form.addRow("Source files", source_row)

        output_dir = [None]
        output_label = QLabel("No target directory selected")
        output_label.setWordWrap(True)
        choose_directory = QPushButton("Choose Folder")

        def choose_target_directory():
            path = QFileDialog.getExistingDirectory(
                dialog, "Choose target folder", self._dialog_dir("batch_target")
            )
            if not path:
                return
            output_dir[0] = Path(path)
            self._remember_dialog_dir("batch_target", path)
            output_label.setText(str(output_dir[0]))

        choose_directory.clicked.connect(choose_target_directory)
        output_row = QHBoxLayout()
        output_row.addWidget(choose_directory)
        output_row.addWidget(output_label, 1)
        form.addRow("Target directory", output_row)
        layout.addLayout(form)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        convert_button = buttons.button(QDialogButtonBox.StandardButton.Ok)
        convert_button.setText("Convert")
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)

        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        if not selected_paths or output_dir[0] is None:
            QMessageBox.warning(
                self, "Mass Convert Fonts", "Choose at least one source file and a target directory."
            )
            return

        source_kind = source_format.currentData()
        target_kind = target_format.currentData()
        prepared = []
        failures = []
        warnings = []
        original_model = self.model
        try:
            for source_path in selected_paths:
                try:
                    model = self._load_batch_source(str(source_path), source_kind)
                    self.model = model
                    issues = self._target_conversion_issues(target_kind)
                    prepared.append((source_path, model, issues))
                    unsupported, oversized, collisions = issues
                    if unsupported or oversized or collisions:
                        warnings.append(source_path.name)
                except Exception as e:
                    failures.append(f"{source_path.name}: {e}")
        finally:
            self.model = original_model

        if warnings:
            reply = QMessageBox.warning(
                self,
                "Character conversion warnings",
                f"{len(warnings)} file(s) contain characters that {target_format.currentText()} "
                "cannot represent. Unsupported glyphs will be omitted and duplicate "
                "character mappings will keep the first glyph.\n\n"
                + "\n".join(warnings[:12])
                + ("\n..." if len(warnings) > 12 else "")
                + "\n\nContinue converting?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if reply != QMessageBox.StandardButton.Yes:
                return

        written = []
        used_paths = set()
        try:
            for source_path, model, _issues in prepared:
                destination = self._batch_output_path(
                    output_dir[0], source_path, target_kind, used_paths
                )
                self.model = model
                if target_kind == "signmatrix":
                    self._write_signmatrix_export(
                        destination,
                        destination.with_suffix(".png"),
                        destination.stem,
                        self._signmatrix_description(destination.stem),
                    )
                else:
                    expected_type = HanoverFont if target_kind == "hanover" else LuminatorFont
                    output_model = (
                        model if isinstance(model, expected_type)
                        else self._convert_model_for_fnt(target_kind)
                    )
                    output_model.save(destination)
                written.append(destination.name)
        except Exception as e:
            failures.append(str(e))
        finally:
            self.model = original_model

        summary = f"Converted {len(written)} of {len(selected_paths)} file(s) to {output_dir[0]}."
        if failures:
            summary += "\n\nSkipped or failed:\n" + "\n".join(failures[:12])
            if len(failures) > 12:
                summary += "\n..."
            QMessageBox.warning(self, "Mass conversion finished", summary)
        else:
            QMessageBox.information(self, "Mass conversion finished", summary)
        self.statusBar().showMessage(summary.split("\n", 1)[0], 6000)

    def save(self):
        if not self.model:
            return

        # No real FNT path yet (new/imported font), so fall back to Save As.
        if self.current_path is None or self._imported_from_signmatrix or self._new_from_scratch:
            self.save_as()
            return

        if isinstance(self.model, LuminatorFont) and not self._confirm_luminator_omissions():
            return

        path_key = str(self.current_path)
        if path_key not in self._confirmed_save_paths:
            reply = QMessageBox.question(
                self,
                "Save file",
                f"Save changes to {self.current_path.name}?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if reply != QMessageBox.StandardButton.Yes:
                return
            self._confirmed_save_paths.add(path_key)

        try:
            self.model.save(str(self.current_path))
        except Exception as e:
            QMessageBox.critical(self, "Save failed", str(e))
            return

        self.dirty = False
        title = "Luminator FNT Pixel Editor"
        if self.current_path:
            title += f" — {self.current_path.name}"
        self.setWindowTitle(title)
        self.statusBar().showMessage(f"Saved {self.current_path.name}", 5000)

    def save_as(self, format_hint=None):
        if not self.model:
            return

        if format_hint == "signmatrix":
            self.export_for_signmatrix()
            return

        if format_hint is None:
            format_hint = "hanover" if isinstance(self.model, HanoverFont) else "luminator"

        expected_model = HanoverFont if format_hint == "hanover" else LuminatorFont
        cross_format = not isinstance(self.model, expected_model)
        if cross_format and not self._confirm_target_character_support(format_hint):
            return

        if (self._imported_from_signmatrix or self._new_from_scratch) and self.current_path:
            default_name = self.current_path.stem + ".fnt"
        else:
            default_name = (
                self.current_path.stem + "_edited.fnt"
                if self.current_path else "edited_font.fnt"
            )
        default_path = str(Path(self._dialog_dir("font_save")) / default_name)
        path, _ = QFileDialog.getSaveFileName(
            self,
            "Save font as",
            default_path,
            "FNT (Hanover) (*.fnt)" if format_hint == "hanover"
            else "FNT (Luminator) (*.fnt)"
        )
        if not path:
            return

        if not cross_format and format_hint == "luminator":
            if not self._confirm_luminator_omissions():
                return

        path = str(Path(path).with_suffix(".fnt"))
        self._remember_dialog_dir("font_save", path)

        try:
            model_to_save = (
                self._convert_model_for_fnt(format_hint)
                if cross_format else self.model
            )
            model_to_save.save(path)
        except Exception as e:
            QMessageBox.critical(self, "Save failed", str(e))
            return

        self.current_path = Path(path)
        if cross_format:
            self.model = model_to_save
        self._imported_from_signmatrix = False
        self._new_from_scratch = False
        self._confirmed_save_paths.add(str(self.current_path))
        self.filename_label.setText(self.current_path.name)
        self.dirty = False
        title = "Luminator FNT Pixel Editor"
        title += f" — {self.current_path.name}"
        self.setWindowTitle(title)
        self.statusBar().showMessage(f"Saved {Path(path).name}", 5000)

    def _select_character_code(self, code):
        if not self.model:
            return
        if not (self.model.first <= code <= self.model.last):
            return

        idx = code - self.model.first
        self.canvas.set_glyph_index(idx)

        ch = getattr(self.model, "display_labels", {}).get(
            code, display_character_for_code(code)
        )
        shown = "SPACE" if code == 0x20 else repr(ch)
        self.selected_char_label.setText(f"0x{code:02X}  {shown}")
        self.refresh_metrics()
        if not self.manual_scale_override:
            QTimer.singleShot(0, self.auto_fit_canvas)

    def character_selection_changed(self):
        if not self.model:
            return

        codes = sorted(
            {
                int(item.data(Qt.ItemDataRole.UserRole))
                for item in self.char_table.selectedItems()
                if item.data(Qt.ItemDataRole.UserRole) is not None
            }
        )
        self._selected_codes = codes
        multi = len(codes) > 1

        self.scroll.setVisible(not multi)
        self.multi_select_label.setVisible(multi)

        for btn in (
            self.add_left_col_btn,
            self.add_right_col_btn,
            self.remove_left_col_btn,
            self.remove_right_col_btn,
            self.add_glyph_btn,
        ):
            btn.setEnabled(not multi)

        if multi:
            self.selected_char_label.setText(f"{len(codes)} selected")
            self.refresh_metrics()
            self.statusBar().showMessage(f"{len(codes)} characters selected", 2500)
            return

        if codes:
            self._select_character_code(codes[0])

    def manual_scale_changed(self, value):
        self.scale_value_label.setText(f"{value}%")
        if not self.model:
            return
        self.manual_scale_override = True
        self.canvas.set_scale_factor(value / 100.0)
        self.statusBar().showMessage(f"Scale: {value}% (manual)", 2500)

    def enable_auto_fit(self):
        self.manual_scale_override = False
        self.auto_fit_canvas()
        self.statusBar().showMessage("Scale: automatic fit", 2500)

    def auto_fit_canvas(self):
        """Use 100% unless it would overflow the painting viewport.

        Once the user manually changes scale, automatic fitting stops until
        the Fit button is pressed (or a new font is opened).
        """
        if not self.model or self.manual_scale_override:
            return

        cols = max(1, self.model.width(self.canvas.glyph_index))
        rows = max(1, self.model.height)

        viewport = self.scroll.viewport().size()
        # Keep a little padding so the matrix never touches the scroll frame.
        available_w = max(1, viewport.width() - 28)
        available_h = max(1, viewport.height() - 28)

        natural_w = cols * self.canvas.base_cell_size + 1
        natural_h = rows * self.canvas.base_cell_size + 1

        fit_factor = min(1.0, available_w / natural_w, available_h / natural_h)
        fit_factor = max(0.15, fit_factor)

        self.canvas.set_scale_factor(fit_factor)
        pct = int(round(fit_factor * 100))
        self.scale_slider.blockSignals(True)
        self.scale_slider.setValue(pct)
        self.scale_slider.blockSignals(False)
        self.scale_value_label.setText(f"{pct}%")

    def keyPressEvent(self, event):
        if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            if event.key() == Qt.Key.Key_Z:
                self.undo()
                event.accept()
                return
            if event.key() == Qt.Key.Key_Y:
                self.redo()
                event.accept()
                return
        super().keyPressEvent(event)

    def wheelEvent(self, event):
        # Ctrl + mouse wheel zooms the pixel editor.
        # Regular wheel events are left to the normal scroll-area behavior.
        if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            delta = event.angleDelta().y()
            if delta == 0:
                event.accept()
                return

            steps = delta / 120.0
            current = self.scale_slider.value()

            # 10% per wheel notch; clamp to slider bounds.
            new_value = int(round(current + steps * 10))
            new_value = max(self.scale_slider.minimum(), min(self.scale_slider.maximum(), new_value))

            if new_value != current:
                self.scale_slider.setValue(new_value)

            event.accept()
            return

        super().wheelEvent(event)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if not self.manual_scale_override:
            QTimer.singleShot(0, self.auto_fit_canvas)
        QTimer.singleShot(0, self._position_selection_actions_bar)

    def changeEvent(self, event):
        super().changeEvent(event)
        # Maximize/restore/snap can settle the window's final geometry a
        # tick after this event, so defer the reposition like resizeEvent does.
        if event.type() == QEvent.Type.WindowStateChange:
            QTimer.singleShot(0, self._position_selection_actions_bar)

    def _snapshot_state(self):
        if not self.model:
            return None
        snapshot = {
            "glyphs": [bytearray(g) for g in self.model.glyphs],
            "spacing": self.model.spacing,
            "glyph_index": self.canvas.glyph_index,
            "first": self.model.first,
            "last": self.model.last,
            "count": self.model.count,
            "height": self.model.height,
            "bytes_per_col": self.model.bytes_per_col,
        }
        if isinstance(self.model, HanoverFont):
            snapshot["advance_widths"] = list(self.model.advance_widths)
        return snapshot

    def _restore_snapshot(self, snap):
        if not self.model or snap is None:
            return
        self._restoring_history = True
        try:
            self.model.glyphs = [bytearray(g) for g in snap["glyphs"]]
            self.model.spacing = snap["spacing"]
            self.model.first = snap["first"]
            self.model.last = snap["last"]
            self.model.count = snap["count"]
            self.model.height = snap["height"]
            self.model.bytes_per_col = snap["bytes_per_col"]
            if isinstance(self.model, HanoverFont):
                self.model.advance_widths = list(snap["advance_widths"])

            idx = max(0, min(snap["glyph_index"], len(self.model.glyphs) - 1))
            self.canvas.glyph_index = idx
            self.canvas.clear_selection()
            self.canvas.update_geometry()
            self.canvas.update()

            self.spacing_spin.blockSignals(True)
            self.spacing_spin.setValue(self.model.spacing)
            self.spacing_spin.blockSignals(False)

            code = self.model.first + idx
            self._rebuild_character_table(select_code=code)
            ch = getattr(self.model, "display_labels", {}).get(
                code, display_character_for_code(code)
            )
            shown = "SPACE" if code == 0x20 else repr(ch)
            self.selected_char_label.setText(f"0x{code:02X}  {shown}")

            self.refresh_metrics()
            if not self.manual_scale_override:
                QTimer.singleShot(0, self.auto_fit_canvas)
        finally:
            self._restoring_history = False

    def _begin_history_action(self):
        if self.model and not self._restoring_history and self._pending_history_snapshot is None:
            self._pending_history_snapshot = self._snapshot_state()

    def _commit_history_action(self):
        if self._restoring_history:
            self._pending_history_snapshot = None
            return
        if self._pending_history_snapshot is not None:
            self.undo_stack.append(self._pending_history_snapshot)
            if len(self.undo_stack) > 200:
                self.undo_stack.pop(0)
            self.redo_stack.clear()
        self._pending_history_snapshot = None
        self.update_undo_redo_buttons()

    def _cancel_history_action(self):
        self._pending_history_snapshot = None

    def _canvas_change_finished(self):
        self._commit_history_action()
        if self.model:
            code = self.model.first + self.canvas.glyph_index
            self._update_character_item_style(code)
        self.mark_dirty()

    def update_undo_redo_buttons(self):
        if hasattr(self, "undo_btn"):
            self.undo_btn.setEnabled(bool(self.undo_stack))
        if hasattr(self, "redo_btn"):
            self.redo_btn.setEnabled(bool(self.redo_stack))

    def undo(self):
        if not self.model or not self.undo_stack:
            return
        current = self._snapshot_state()
        snap = self.undo_stack.pop()
        if current is not None:
            self.redo_stack.append(current)
        self._restore_snapshot(snap)
        self.mark_dirty()
        self.update_undo_redo_buttons()
        self.statusBar().showMessage("Undo", 1200)

    def redo(self):
        if not self.model or not self.redo_stack:
            return
        current = self._snapshot_state()
        snap = self.redo_stack.pop()
        if current is not None:
            self.undo_stack.append(current)
        self._restore_snapshot(snap)
        self.mark_dirty()
        self.update_undo_redo_buttons()
        self.statusBar().showMessage("Redo", 1200)

    def font_height_changed(self, value):
        if not self.model or self._restoring_history:
            return

        new_height = int(value)
        old_height = self.model.height
        if new_height == old_height:
            return

        # Shrinking can destroy pixels. Warn only when the rows being removed
        # actually contain lit data anywhere in the font.
        if (
            new_height < old_height
            and self.model.bottom_rows_have_pixels(new_height)
        ):
            reply = QMessageBox.warning(
                self,
                "Discard Pixel Data?",
                f"Reducing the font height from {old_height} to {new_height} pixels "
                "will discard non-blank pixel data from the bottom of one or more glyphs.\n\n"
                "Continue?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if reply != QMessageBox.StandardButton.Yes:
                self.height_value.blockSignals(True)
                self.height_value.setValue(old_height)
                self.height_value.blockSignals(False)
                return

        self._begin_history_action()
        try:
            self.model.resize_height(new_height)
        except Exception as e:
            self._cancel_history_action()
            self.height_value.blockSignals(True)
            self.height_value.setValue(old_height)
            self.height_value.blockSignals(False)
            QMessageBox.critical(self, "Cannot Change Font Height", str(e))
            return

        self._commit_history_action()

        self.canvas.clear_selection()
        self.canvas.update_geometry()
        self.canvas.update()
        self._rebuild_character_table(
            select_code=self.model.first + self.canvas.glyph_index
        )
        self.refresh_metrics()
        self.mark_dirty()

        if not self.manual_scale_override:
            QTimer.singleShot(0, self.auto_fit_canvas)

    def spacing_changed(self, value):
        if not self.model or self._restoring_history:
            return
        if self.model.spacing != value:
            self._begin_history_action()
            self.model.spacing = value
            self._commit_history_action()
            self.mark_dirty()
            self.refresh_metrics()

    def _add_column(self, side):
        if not self.model:
            return
        self._begin_history_action()
        idx = self.canvas.glyph_index
        self.model.add_column(idx, side)
        self.canvas.clear_selection()
        self.canvas.update_geometry()
        self.canvas.update()
        self._commit_history_action()
        self.mark_dirty()
        self.refresh_metrics()
        if not self.manual_scale_override:
            QTimer.singleShot(0, self.auto_fit_canvas)

    def add_left_column(self):
        self._add_column("left")

    def add_right_column(self):
        self._add_column("right")

    def _remove_column(self, side):
        if not self.model:
            return
        idx = self.canvas.glyph_index
        self._begin_history_action()
        if not self.model.remove_column(idx, side):
            self._cancel_history_action()
            self.statusBar().showMessage("A glyph must keep at least one column.", 2500)
            return

        self.canvas.clear_selection()
        self.canvas.update_geometry()
        self.canvas.update()
        self._commit_history_action()
        self.mark_dirty()
        self.refresh_metrics()
        if not self.manual_scale_override:
            QTimer.singleShot(0, self.auto_fit_canvas)

    def remove_left_column(self):
        self._remove_column("left")

    def remove_right_column(self):
        self._remove_column("right")

    def add_glyph(self):
        if not self.model:
            return

        default_code = self.model.last + 1 if self.model.last < 255 else self.model.first - 1
        default_text = (
            chr(default_code) if 32 <= default_code <= 126 else f"0x{default_code:02X}"
        )

        text, ok = QInputDialog.getText(
            self,
            "Add glyph",
            "Character to add (single character or hex such as 0x7E):",
            text=default_text,
        )
        if not ok:
            return

        text = text.strip()
        if not text:
            return

        try:
            if text.lower().startswith("0x"):
                code = int(text, 16)
            elif len(text) == 1:
                code = ord(text)
            else:
                raise ValueError
        except ValueError:
            QMessageBox.warning(
                self,
                "Invalid character",
                "Enter exactly one character, or a byte value such as 0x7E."
            )
            return

        if not 0 <= code <= 255:
            QMessageBox.warning(
                self,
                "Unsupported character",
                "Luminator FNT character codes are limited to 0x00 through 0xFF."
            )
            return

        if self.model.first <= code <= self.model.last:
            QMessageBox.information(
                self,
                "Glyph already exists",
                f"0x{code:02X} ({repr(chr(code))}) is already inside the font's "
                "character range."
            )
            return

        gap = (
            self.model.first - code
            if code < self.model.first
            else code - self.model.last
        )
        if gap > 1:
            reply = QMessageBox.question(
                self,
                "Extend character range?",
                f"Luminator FNT stores a continuous character range.\n\n"
                f"Adding 0x{code:02X} will also create {gap - 1} blank placeholder "
                f"glyph{'s' if gap - 1 != 1 else ''} for the intervening character "
                f"code{'s' if gap - 1 != 1 else ''}.\n\nContinue?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if reply != QMessageBox.StandardButton.Yes:
                return

        self._begin_history_action()
        try:
            self.model.add_glyph(code, width=1)
        except Exception as e:
            self._cancel_history_action()
            QMessageBox.critical(self, "Cannot add glyph", str(e))
            return

        self._commit_history_action()
        self._rebuild_character_table(select_code=code)

        idx = code - self.model.first
        self.canvas.set_glyph_index(idx)
        self._select_character_code(code)
        self.canvas.update_geometry()
        self.canvas.update()
        self.mark_dirty()
        self.refresh_metrics()

        if not self.manual_scale_override:
            QTimer.singleShot(0, self.auto_fit_canvas)

    def remove_glyph(self):
        if not self.model:
            return

        if len(self._selected_codes) > 1:
            self._remove_glyph_multi(self._selected_codes)
            return

        idx = self.canvas.glyph_index
        code = self.model.first + idx
        ch = chr(code)

        if code != self.model.first and code != self.model.last:
            reply = QMessageBox.question(
                self,
                "Interior glyph cannot be removed",
                f"{repr(ch)} (0x{code:02X}) is inside the font's character range.\n\n"
                "Luminator FNT requires every character code between the first "
                "and last glyph to have a slot, so removing this glyph would "
                "renumber other characters.\n\n"
                "Clear this glyph to blank instead?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if reply == QMessageBox.StandardButton.Yes:
                self._begin_history_action()
                self.model.glyphs[idx] = bytearray(
                    max(1, self.model.width(idx)) * self.model.bytes_per_col
                )
                self.canvas.clear_selection()
                self.canvas.update()
                self._commit_history_action()
                self._update_character_item_style(code)
                self.mark_dirty()
            return

        reply = QMessageBox.question(
            self,
            "Remove glyph",
            f"Remove {repr(ch)} (0x{code:02X}) from the font?\n\n"
            "This changes the font's first/last character range.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return

        self._begin_history_action()
        ok, message = self.model.remove_edge_glyph(code)
        if not ok:
            self._cancel_history_action()
            QMessageBox.warning(self, "Cannot remove glyph", message)
            return

        self._commit_history_action()

        # Select the new nearest edge glyph.
        new_code = self.model.first if code < self.model.first else self.model.last
        self._rebuild_character_table(select_code=new_code)
        self.canvas.set_glyph_index(new_code - self.model.first)
        self._select_character_code(new_code)
        self.canvas.update_geometry()
        self.canvas.update()
        self.mark_dirty()
        self.refresh_metrics()

        if not self.manual_scale_override:
            QTimer.singleShot(0, self.auto_fit_canvas)

    def clear_glyph(self):
        if not self.model:
            return

        if len(self._selected_codes) > 1:
            self._clear_glyph_multi(self._selected_codes)
            return

        idx = self.canvas.glyph_index
        reply = QMessageBox.question(
            self,
            "Clear glyph",
            "Clear every pixel in this glyph?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return

        self._begin_history_action()
        self.model.glyphs[idx] = bytearray(len(self.model.glyphs[idx]))
        self.canvas.clear_selection()
        self.canvas.update()
        self._commit_history_action()
        code = self.model.first + idx
        self._update_character_item_style(code)
        self.mark_dirty()

    def _clear_glyph_multi(self, codes):
        reply = QMessageBox.question(
            self,
            "Clear glyphs",
            f"Clear every pixel in the {len(codes)} selected glyphs?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return

        self._begin_history_action()
        for code in codes:
            if self.model.first <= code <= self.model.last:
                idx = code - self.model.first
                self.model.glyphs[idx] = bytearray(len(self.model.glyphs[idx]))
        self.canvas.clear_selection()
        self.canvas.update()
        self._commit_history_action()

        for code in codes:
            self._update_character_item_style(code)
        self.mark_dirty()
        self.statusBar().showMessage(f"Cleared {len(codes)} glyphs", 2500)

    def _remove_glyph_multi(self, codes):
        reply = QMessageBox.question(
            self,
            "Remove glyphs",
            f"Remove the {len(codes)} selected characters?\n\n"
            "Characters at the edge of the font's character range will be removed "
            "entirely; characters inside the range will be cleared to blank instead, "
            "because Luminator FNT requires one contiguous character range.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return

        remaining = set(codes)
        self._begin_history_action()

        # Peel edge codes off the range one at a time; whatever is left after
        # the range can no longer shrink around it gets cleared instead.
        progressed = True
        while progressed and remaining:
            progressed = False
            if self.model.count > 1 and self.model.first in remaining:
                old_first = self.model.first
                ok, _ = self.model.remove_edge_glyph(old_first)
                if ok:
                    remaining.discard(old_first)
                    progressed = True
                    continue
            if self.model.count > 1 and self.model.last in remaining:
                old_last = self.model.last
                ok, _ = self.model.remove_edge_glyph(old_last)
                if ok:
                    remaining.discard(old_last)
                    progressed = True
                    continue

        for code in remaining:
            if self.model.first <= code <= self.model.last:
                idx = code - self.model.first
                self.model.glyphs[idx] = bytearray(len(self.model.glyphs[idx]))

        self._commit_history_action()

        new_code = min(self.model.last, max(self.model.first, codes[0]))
        self._rebuild_character_table(select_code=new_code)
        self.canvas.set_glyph_index(new_code - self.model.first)
        self._select_character_code(new_code)
        self.canvas.update_geometry()
        self.canvas.update()
        self.mark_dirty()
        self.refresh_metrics()

        if not self.manual_scale_override:
            QTimer.singleShot(0, self.auto_fit_canvas)

    def mark_dirty(self):
        if not self.model:
            return
        self.dirty = True
        title = "Luminator FNT Pixel Editor"
        if self.current_path:
            title += f" — {self.current_path.name}"
        if self.dirty:
            title += " *"
        self.setWindowTitle(title)

    def refresh_metrics(self):
        if not self.model:
            self.height_value.setEnabled(False)
            self.width_value.setText("—")
            self.bytes_value.setText("—")
            return

        if len(self._selected_codes) > 1:
            self.height_value.blockSignals(True)
            self.height_value.setEnabled(True)
            self.height_value.setValue(self.model.height)
            self.height_value.blockSignals(False)
            self.width_value.setText("—")
            self.bytes_value.setText(str(self.model.bytes_per_col))
            self.glyph_title.setText(f"Pixel Editor · {len(self._selected_codes)} selected")
            return

        idx = self.canvas.glyph_index
        code = self.model.first + idx
        width = self.model.width(idx)

        self.height_value.blockSignals(True)
        self.height_value.setEnabled(True)
        self.height_value.setValue(self.model.height)
        self.height_value.blockSignals(False)
        self.width_value.setText(f"{width} px")
        self.bytes_value.setText(str(self.model.bytes_per_col))
        ch = getattr(self.model, "display_labels", {}).get(
            code, display_character_for_code(code)
        )
        self.glyph_title.setText(f"Pixel Editor · {repr(ch)}")

    def closeEvent(self, event):
        if self.dirty:
            reply = QMessageBox.question(
                self,
                "Unsaved changes",
                "You have unsaved changes. Close anyway?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if reply != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
        event.accept()


def main():
    app = QApplication(sys.argv)
    app.setApplicationName("Luminator FNT Pixel Editor")
    app.setFont(QFont("Segoe UI", 10))
    window = MainWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
