# Luminator FNT Editor

A Windows editor for Luminator IPS bitmap font (`.fnt`) files. As of v1.2.0, Luminator MIE bitmap font (`.fnt`), Hanover HELEN bitmap font (`.fnt`) and Axion DataTransit bitmap font (`.bbm`) are also supported.

This project was created through reverse engineering of font files used by Luminator IPS.

## Features

- Open and edit Luminator IPS, Luminator MIE, and Hanover `.fnt` files; import Axion `.bbm` and [SignMatrix](https://github.com/itzzmarkus/Signmatrix) `.json` + `.png`
- Save Luminator IPS, Luminator MIE (only from MIE imports), and Hanover `.fnt` files; export [SignMatrix](https://github.com/itzzmarkus/Signmatrix) `.json` + `.png`
- Exported Luminator IPS and Hanover fonts can be imported back to Luminator IPS or Hanover HELEN
- Create new Luminator IPS `.fnt` files from scratch
- Pixel-level glyph editor
- Extract embedded `.fnt` files from Luminator IPS databases (`.ips`); Requires a compatible Microsoft Access database provider
(ACE or Jet) to be installed on Windows
- Mass convert between supported formats

## Format Information

### Legacy Luminator IPS FNT
The legacy Luminator IPS `.fnt` format is a proprietary bitmap font format and is not the standard Microsoft Windows FNT format. It is distinct from the newer Luminator MIE `.fnt` format below. The format has been reverse engineered from existing Luminator IPS font files.

Known characteristics include:

- Font height stored in the file header
- Global inter-character spacing
- First and last character codes
- Variable-width glyphs
- 16-bit big-endian glyph offset table
- Column-major bitmap storage
- Multi-byte vertical columns for fonts taller than 8 pixels

Some parts of the format may still be undocumented.

### Luminator MIE FNT
Luminator MIE `.fnt` files are a newer proprietary Luminator bitmap font format and are not interchangeable with legacy Luminator IPS `.fnt` files or standard Microsoft Windows FNT files.

Known characteristics include:

- `00 03` file signature and a 148-byte binary header
- Font height, maximum glyph width, global inter-character spacing, and first and last character codes in the header
- Variable-width glyph records containing a 16-bit little-endian width and a 32-bit little-endian absolute bitmap offset
- Bitmap rows grouped into 8-column planes, with most-significant-bit-first pixels
- Optional zero-width glyph records that still reserve a blank bitmap plane
- Format-specific data between the glyph directory and bitmap data, plus an optional trailing data block

The editor opens MIE files through **Open > MIE (.FNT)**; files with the MIE signature are also detected automatically. Imported MIE fonts can be edited and saved as native MIE `.fnt` files. An unchanged open/save round trip preserves the supplied MIE files byte-for-byte. Native MIE saving is available for fonts imported from MIE; conversion from other font formats to newly synthesized MIE files is not currently supported.

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