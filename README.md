# Yokaze (夜風)

One-button document scanning for Linux. Works with [Fujitsu ScanSnap](https://www.pfu.ricoh.com/global/scanners/scansnap/) scanners the SANE `fujitsu` backend exposes as `fujitsu:ScanSnap …` (tested with the iX1300).

Press the scanner button → get a PDF.

## Features

- **One-button scanning** — hardware button or in-app Scan
- **Desktop app** — Scan, History, Stage, Paperless, Settings
- **Feeder or front slot** — uses whichever input has a document
- **Multi-page documents** — keep scanning; combine into one PDF
- **Duplex A4** — both sides automatically
- **Smart blank detection** — drop empty duplex backs
- **Color / Grayscale / Auto**
- **Paperless-ngx API or folder delivery**
- **Send to NAPS2**

## Requirements

- **just**, **sane-backends**, **tesseract** (OSD orientation)
- **libnotify**, **xdg-utils**, **xdg-terminal-exec**
- **python3** + **PyGObject**, **GTK 4**, **libadwaita**
- **python-opencv** (`cv2`) and **python-pillow**
- **NAPS2** (optional)

Run `just check` to see which dependencies are installed.

## Installation

### Arch Linux (AUR)

When published (see packaging notes in the workbench):

```bash
yay -S yokaze
```

### From source

```bash
just install   # launchers, Yokaze desktop icon, systemd, udev
just check
```

After install, open **Yokaze** from your app menu. Use **Settings** to choose a scanner, configure Paperless, and optionally pick an archives folder (default: `~/.local/share/yokaze`).

To reuse archives from an older ScanSnap-era checkout:

```bash
mkdir -p ~/.local/share/yokaze
mv /path/to/old/data/archives ~/.local/share/yokaze/archives
# or: mv ~/.local/share/scansnap/archives ~/.local/share/yokaze/archives
```

## Usage

```bash
just gui                         # desktop app
./yokaze --tab history
yokaze-scan                      # headless one-button
```

The hardware-button service runs `yokaze-button`. When the app is open, the button uses the current Scan selection; when closed, it runs a headless single-page delivery.

## License

MIT — see [LICENSE](LICENSE).
