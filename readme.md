# Luminator FNT Editor

A Windows editor for Luminator IPS bitmap font (`.fnt`) files.

This project was created through reverse engineering of font files used by Luminator IPS.

## Features

- Open and edit Luminator `.fnt` bitmap fonts or create new `.fnt` files from scratch
- Pixel-level glyph editor
- Extract embedded `.fnt` files from Luminator IPS databases (`.ips`). Requires a compatible Microsoft Access database provider
(ACE or Jet) to be installed on Windows.
- **Import and export support for [SignMatrix](https://github.com/itzzmarkus/Signmatrix)/[dotmatrix](https://github.com/chrislo27/dotmatrix/) JSON/PNG format**

## Luminator FNT Format

The Luminator .fnt format used by this editor is a proprietary bitmap font format and is not the standard Microsoft Windows FNT format. The format has been reverse engineered from existing Luminator font files.

Known characteristics include:

- Font height stored in the file header
- Global inter-character spacing
- First and last character codes
- Variable-width glyphs
- 16-bit big-endian glyph offset table
- Column-major bitmap storage
- Multi-byte vertical columns for fonts taller than 8 pixels

Some parts of the format may still be undocumented.

## Disclaimer

This project is *not affiliated* with Luminator Technology Group, nor the SignMatrix and dotmatrix projects.