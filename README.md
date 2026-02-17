# Downloadarr

Downloadarr is a Python-based synchronization tool designed to bridge the gap between a remote rTorrent instance and a local media server. It monitors rTorrent for completed downloads, transfers them via FTP to your local storage, and can trigger post-processing actions like notifying Sonarr or Radarr.

## Features

- **rTorrent Integration**: Connects via XMLRPC to monitor torrent status and labels.
- **FTP Synchronization**: Downloads files from remote servers with support for resuming interrupted transfers.
- **Smart Filtering**: Skip files based on size, file extensions, or regular expressions.
- **Priority Management**: Process specific labels first based on configurable priorities.
- **Arrapi Integration**: Automatically triggers Sonarr/Radarr library scans after a successful download.
- **Notifications**: Sends status updates via Telegram.
- **Permission Control**: Automatically sets folder and file permissions/ownership after download.
- **Docker Ready**: Easy deployment using Docker and Docker Compose.

## Prerequisites

- Python 3.9+
- An rTorrent instance with XMLRPC enabled.
- An FTP server providing access to your rTorrent download directory.

## Installation

### Using Docker (Recommended)

1. Clone this repository.
2. Copy `config.yaml.example` to `config.yaml` and fill in your details.
3. Run with Docker Compose:

```bash
docker-compose up -d
```

### Manual Installation

1. Clone the repository.
2. Create a virtual environment and install dependencies:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

3. Copy `config.yaml.example` to `config.yaml` and configure it.
4. Run the script:

```bash
python3 downloadarr.py
```

## Configuration

The `config.yaml` file is the heart of Downloadarr. Key sections include:

- **ftp**: Connection details for your FTP server.
- **rtorrent**: XMLRPC connection details for rTorrent.
- **folders**: Define your local root, temporary download path, and permission settings.
- **label_mapping**: Map rTorrent labels to local subdirectories and define post-download actions.
- **rules**: Define global download rules (min/max size, skip patterns).

See `config.yaml.example` for a documented example.

## Usage & Arguments

```bash
usage: downloadarr.py [-h] [--dry-run] [--one-shot] [--debug] [--config CONFIG]
                      [--skip-extensions SKIP_EXTENSIONS] [--dont-change-label]
                      [--min-file-size MIN_FILE_SIZE] [--max-file-size MAX_FILE_SIZE]
                      [--skip-regex SKIP_REGEX] [--allow-multiple-instances]
                      [--pid-file PID_FILE]

optional arguments:
  -h, --help            show this help message and exit
  --dry-run             Simulate the download process without actually downloading
  --one-shot            Run the script only once without looping
  --debug               Enable debug mode
  --config CONFIG       Path to the config file (default: config.yaml)
  --skip-extensions SKIP_EXTENSIONS
                        Comma-separated list of file extensions to skip
  --dont-change-label   Don't change the label of the torrents when download completes
  --min-file-size MIN_FILE_SIZE
                        Minimum file size to download (in bytes)
  --max-file-size MAX_FILE_SIZE
                        Maximum file size to download (in bytes)
  --skip-regex SKIP_REGEX
                        Comma-separated list of regex patterns to skip
  --allow-multiple-instances
                        Allow multiple instances of the script to run
  --pid-file PID_FILE   Path to the PID file
```

## Post-Download Actions

Currently supported actions in `label_mapping`:

- `notify_radarr`: Triggers a "DownloadedMoviesScan" in Radarr.
- `notify_sonarr`: Triggers a "DownloadedEpisodesScan" in Sonarr.

## License

MIT
