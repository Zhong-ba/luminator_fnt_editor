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

Known characteristics include:

- 111 fixed-size glyph slots with no file header, covering character codes `0x20` through `0x8E`
- Slot `0x20` is the stored blank-but-width-bearing space glyph
- Each slot begins with an encoded width followed by packed, column-major bitmap data
- One- through four-byte vertical-column encodings, inferred from the encoded widths
- Glyph height inferred from the filename (`5x7`, `OUTLINE-14`, or `IMP-LUM-18`); files without a recognized height use their packed-column capacity
- Outline fonts are two pixels taller than their numeric base height, capped by their stored column capacity

BBM does not contain a confirmed file-level character spacing value. The importer currently uses a legacy default of one pixel below 10 pixels high and two pixels at 10 pixels or higher.

## Conversion Notes

- Luminator output is limited to printable ASCII characters (`0x20`-`0x7E`).
- Hanover output is limited to Windows-1252 characters in the `0x20`-`0xFF` range and 32-pixel-wide glyphs.
- SignMatrix output accepts printable Unicode labels.
- Warnings will appear before conversion when the target format cannot represent a source glyph, when glyphs collide at a target character code, or when a Hanover glyph is too wide. Unsupported glyphs are omitted only after confirmation.

## Disclaimer

This project is *not affiliated* with Luminator Technology Group, Hanover Displays, Axion Technologies, nor the SignMatrix and dotmatrix projects.