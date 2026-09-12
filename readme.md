# Luminator FNT Editor

A Windows editor for Luminator IPS bitmap font (`.fnt`) files. As of v1.1.0, Hanover HELEN bitmap font (`.fnt`) and Axion DataTransit bitmap font (`.bbm`) are also supported.

This project was created through reverse engineering of font files used by Luminator IPS.

## Features

- Import bitmap fonts from Luminator `.fnt`, Hanover `.fnt`, Axion `.bbm`, and [SignMatrix](https://github.com/itzzmarkus/Signmatrix) `.json` + `.png`
- Export bitmap fonts as Luminator `.fnt`, Hanover `.fnt`, and [SignMatrix](https://github.com/itzzmarkus/Signmatrix) `.json` + `.png`; Exporting to Axion `.bbm` is not supported
- Exported fonts can be imported back to Luminator IPS or Hanover HELEN
- Create new Luminator `.fnt` from scratch
- Pixel-level glyph editor
- Extract embedded `.fnt` files from Luminator IPS databases (`.ips`); Requires a compatible Microsoft Access database provider
(ACE or Jet) to be installed on Windows
- Mass convert between supported formats

## Format Information

### Luminator FNT
The Luminator `.fnt` format is a proprietary bitmap font format and is not the standard Microsoft Windows FNT format. The format has been reverse engineered from existing Luminator font files.

Known characteristics include:

- Font height stored in the file header
- Global inter-character spacing
- First and last character codes
- Variable-width glyphs
- 16-bit big-endian glyph offset table
- Column-major bitmap storage
- Multi-byte vertical columns for fonts taller than 8 pixels

Some parts of the format may still be undocumented.

### Hanover FNT
The Hanover `.fnt` format are a proprietary text-based bitmap font format and are not standard Microsoft Windows FNT files. The fonts use Windows-1252 character labels from `0x20` through `0xFF`.

Known characteristics include:

- Five decimal header values: format mode, height, maximum glyph width, character-set value, and inter-character spacing
- A 224-entry character-width table for the `0x20`-`0xFF` character range
- One decimal text record per glyph
- Fixed-width records containing packed 32-bit vertical bitmap columns
- Variable glyph widths, including blank-but-width-bearing space characters
- Native Windows CRLF line endings for HELEN compatibility

The editor supports header mode values `0` and `2`, font heights up to 32 pixels, and glyph widths up to 32 pixels.

### Axion BBM
Axion DataTransit `.bbm` files are fixed-slot binary bitmap fonts. They are currently import-only and not possible to write.

The importer supports a limited number of observed BBM layouts only. It identifies a layout from its file size, reads each glyph's stored width and padded bitmap slot, and handles both one-byte and two-byte vertical-column encodings. For most files, the glyph height is determined from a filename containing a dimension such as `5x7`.

## Conversion Notes

- Luminator output is limited to printable ASCII characters (`0x20`-`0x7E`).
- Hanover output is limited to Windows-1252 characters in the `0x20`-`0xFF` range and 32-pixel-wide glyphs.
- SignMatrix output accepts printable Unicode labels.
- Warnings will appear before conversion when the target format cannot represent a source glyph, when glyphs collide at a target character code, or when a Hanover glyph is too wide. Unsupported glyphs are omitted only after confirmation.

## Disclaimer

This project is *not affiliated* with Luminator Technology Group, Hanover Displays, Axion Technologies, nor the SignMatrix and dotmatrix projects.