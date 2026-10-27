# New Device Setup Guide

This document outlines the steps to set up the `video-dedupe` (The File Hashing Slasher) project on a new device. The project has been audited and prepared for cross-device portability.

## Prerequisites
- **Operating System:** Linux (Zorin OS, Ubuntu, or other Debian-based distributions).
- **Python:** Python 3.12 or newer.
- **System Dependencies:** `ffmpeg` (for video hashing and thumbnails) and `python3-venv`.

## Step-by-Step Installation

### 1. Clone or Copy the Repository
Transfer the project directory to your new device. 
Ensure that `.venv`, `__pycache__`, and other local cache directories were excluded from the transfer (as defined in `.gitignore`), or delete them if they were copied over:
```bash
rm -rf .venv __pycache__
```

### 2. Install System Dependencies & Setup Python Environment
The `setup.sh` script automates the installation of system packages and Python dependencies. It requires `sudo` privileges to install `ffmpeg` and Python virtual environment tools via `apt`.

Run the following command from the project root:
```bash
./setup.sh
```

**What this script does:**
1. Runs `sudo apt update` and installs `ffmpeg`, `python3`, `python3-venv`, and `python3-pip`.
2. Creates a clean Python virtual environment (`.venv`).
3. Installs all project dependencies from `requirements.txt`.
4. Creates a desktop shortcut and installs the app icon in your `~/.local/share` directory so it appears in your application menu.

### 3. Environment Variables (Optional)
The application follows the XDG Base Directory Specification. If you wish to override the default directories for config, cache, state, or data, you can create a `.env` file or export them in your shell. 
See `.env.example` for details:
```bash
cp .env.example .env
# Edit .env as needed
```

### 4. Running the Application
You can launch the application via the desktop shortcut ("The File Hashing Slasher") or via the command line:
```bash
./run.sh
```

### 5. Running the Test Suite
To verify the application's core logic works correctly on the new device, run the test suite. 

First, ensure development dependencies are installed:
```bash
source .venv/bin/activate
pip install -r requirements-dev.txt
```

Then, run the tests:
```bash
python -m unittest discover -s tests
```
Or, if using pytest:
```bash
pytest tests/
```

## Troubleshooting
- **Missing `ffmpeg`:** If video scanning fails, ensure `ffmpeg` is installed (`ffmpeg -version`) and accessible in your system `$PATH`.
- **Desktop Shortcut not appearing:** Log out and log back in, or restart your desktop environment. You can also manually run `update-desktop-database ~/.local/share/applications`.
- **PyQt6/PySide6 errors:** Ensure you are running a Wayland/X11 session compatible with Qt6. Missing system libraries for Qt might require installing `libegl1` or `libxkbcommon-x11-0`.
