import yaml
import ftputil
import os
import argparse
import xmlrpc.client
import time
import colorlog
import re
import pickle
import sys
import grp
import socket
import requests
import unicodedata
from dataclasses import dataclass
from enum import Enum

from arrapi import SonarrAPI
from arrapi import RadarrAPI

global logger
global config

# TODO: Trigger import after download
# TODO: Unarchive automatically and remove archives
# TODO: Webhook for external scripts
# TODO: Handle incomplete downloads
# TODO: Handle error setting label and better error handling in general

# Configure colorlog
handler = colorlog.StreamHandler()
handler.setFormatter(
    colorlog.ColoredFormatter(
        "%(log_color)s%(levelname)-8s%(reset)s %(message)s",
        log_colors={
            "DEBUG": "cyan",
            "INFO": "green",
            "WARNING": "yellow",
            "ERROR": "red",
            "CRITICAL": "red,bg_white",
        },
    )
)

class FileTransferResult(Enum):
    DOWNLOADED = "downloaded"
    SKIPPED = "skipped"
    EXISTS = "exists"
    FAILED = "failed"


@dataclass
class MirrorStats:
    downloaded: int = 0
    skipped: int = 0
    exists: int = 0
    failed: int = 0

    @property
    def success(self) -> bool:
        return self.failed == 0

    @property
    def has_content(self) -> bool:
        return self.downloaded > 0 or self.exists > 0

    def record(self, result: FileTransferResult) -> None:
        if result == FileTransferResult.DOWNLOADED:
            self.downloaded += 1
        elif result == FileTransferResult.SKIPPED:
            self.skipped += 1
        elif result == FileTransferResult.EXISTS:
            self.exists += 1
        elif result == FileTransferResult.FAILED:
            self.failed += 1


def ensure_unicode_path(path, encoding="utf-8"):
    if path is None:
        return ""
    if isinstance(path, bytes):
        path = path.decode(encoding, errors="replace")
    elif not isinstance(path, str):
        path = str(path)
    return unicodedata.normalize("NFC", path)


def resolve_torrent_source_directory(torrent_directory, torrent_name):
    directory = ensure_unicode_path(torrent_directory).rstrip("/")
    name = ensure_unicode_path(torrent_name)
    if os.path.basename(directory) == name:
        return directory
    return f"{directory}/{name}"


def build_rtorrent_server_url(rtorrent_config):
    return (
        f"https://{rtorrent_config['user']}:{rtorrent_config['pass']}"
        f"@{rtorrent_config['host']}:{rtorrent_config['port']}{rtorrent_config['path']}"
    )


def connect_rtorrent_server(rtorrent_config):
    return xmlrpc.client.Server(build_rtorrent_server_url(rtorrent_config))


def get_ftp_encoding(cfg=None):
    cfg = cfg if cfg is not None else config
    return cfg.get("ftp", {}).get("encoding", "utf-8")



def load_config(file_path):
    """
    Load the YAML configuration file.

    Args:
        file_path (str): Path to the configuration file.

    Returns:
        dict: Configuration data.
    """
    with open(file_path, "r", encoding="utf-8") as file:
        return yaml.safe_load(file)


def initialize_logger():
    """
    Initialize the logger with the specified configuration.

    Returns:
        None
    """
    global config
    logger = colorlog.getLogger()
    logger.addHandler(handler)
    log_level = config.get("logging", {}).get("severity", "DEBUG").upper()
    logger.setLevel(log_level)
    return logger


def send_telegram_notification(config, message):
    """
    Send a notification to Telegram.

    Args:
        config (dict): Configuration dictionary.
        message (str): Message to send.

    Returns:
        None
    """
    telegram_config = config.get("notifications", {}).get("telegram")
    if not telegram_config:
        return

    token = telegram_config.get("token")
    chat_id = telegram_config.get("chat_id")

    if not token or not chat_id:
        logger.warning("Telegram notification enabled but token or chat_id missing.")
        return

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    data = {"chat_id": chat_id, "text": message}
    try:
        response = requests.post(url, data=data, timeout=10)
        response.raise_for_status()
        logger.info("Telegram notification sent.")
    except Exception as e:
        logger.error(f"Failed to send Telegram notification: {e}")


def human_readable_size(size, decimal_places=2):
    """
    Convert a size in bytes to a human-readable format.

    Args:
        size (int): Size in bytes.
        decimal_places (int): Number of decimal places for formatting.

    Returns:
        str: Human-readable size.
    """
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if size < 1024.0:
            return f"{size:.{decimal_places}f} {unit}"
        size /= 1024.0
    return f"{size:.{decimal_places}f} PB"


def download_ftp_file(ftp_host, remote_path, local_path, temp_path, overwrite=False):
    """
    Download a file from the FTP server to a temporary location, then move it to the final destination.
    Includes retry logic for timeouts and size verification.

    Args:
        ftp_host (ftputil.FTPHost): FTP host object.
        remote_path (str): Path to the remote file.
        local_path (str): Path to the local file.
        temp_path (str): Path to the temporary file.
        overwrite (bool): Whether to overwrite the existing file.

    Returns:
        FileTransferResult: Outcome of the transfer attempt.
    """
    encoding = get_ftp_encoding()
    remote_path = ensure_unicode_path(remote_path, encoding)
    local_path = ensure_unicode_path(local_path, encoding)
    temp_path = ensure_unicode_path(temp_path, encoding)

    padded_name = os.path.basename(remote_path)
    if len(padded_name) > 60:
        padded_name = f"{padded_name[:20]}...{padded_name[-40:]}"
    padded_name = padded_name.ljust(70)

    if not os.path.exists(local_path) or overwrite:
        logger.debug(f"Downloading file {remote_path} to {temp_path}")
        temp_dir_path = os.path.dirname(temp_path)
        if not os.path.exists(temp_dir_path):
            os.makedirs(temp_dir_path)
            logger.debug(f"Created directory: {temp_dir_path}")

        max_retries = config.get("ftp", {}).get("retries", 3)
        attempt = 0
        
        try:
            # Get remote size once
            remote_size = ftp_host.path.getsize(remote_path)
            
            # Rules checks
            if remote_size > config["rules"]["max_file_size"]:
                logger.warning(f"\t- {padded_name} [SKIPPED: too big]")
                return FileTransferResult.SKIPPED
            if remote_size < config["rules"]["min_file_size"]:
                logger.warning(f"\t- {padded_name} [SKIPPED: too small]")
                return FileTransferResult.SKIPPED
            if any(
                re.search(pattern, remote_path) for pattern in config["rules"]["skip_regex"]
            ):
                logger.warning(f"\t- {padded_name} [SKIPPED: regex]")
                return FileTransferResult.SKIPPED
            if any(
                remote_path.endswith(ext) for ext in config["rules"]["skip_extensions"]
            ):
                logger.warning(f"\t- {padded_name} [SKIPPED: extension]")
                return FileTransferResult.SKIPPED

            while attempt <= max_retries:
                try:
                    rest_offset = 0
                    if os.path.exists(temp_path):
                        rest_offset = os.path.getsize(temp_path)
                    
                    if remote_size == 0:
                        open(temp_path, "wb").close()
                        logger.debug(f"Created empty file: {temp_path}")
                        break

                    if rest_offset >= remote_size and remote_size > 0:
                        logger.debug(f"Temp file already complete: {temp_path}")
                        break

                    mode = "ab" if rest_offset > 0 else "wb"
                    if rest_offset > 0:
                        logger.debug(f"Resuming {remote_path} from {rest_offset} bytes (Attempt {attempt+1}/{max_retries+1})")
                    
                    with ftp_host.open(remote_path, "rb", rest=rest_offset) as remote_file:
                        logger.info(f"\t+ {padded_name} [OK] (Attempt {attempt+1})")
                        start_time = time.time()
                        with open(temp_path, mode) as local_file:
                            downloaded = rest_offset
                            block_size = 8192
                            while True:
                                buffer = remote_file.read(block_size)
                                if not buffer:
                                    break
                                downloaded += len(buffer)
                                local_file.write(buffer)
                                
                                # Progress reporting
                                if remote_size > 0 and (
                                    downloaded % (block_size * 10) == 0
                                    or downloaded == remote_size
                                ):
                                    percentage = (downloaded / remote_size) * 100
                                    elapsed_time = time.time() - start_time
                                    if (downloaded - rest_offset) > 0:
                                        eta = ((elapsed_time / (downloaded - rest_offset)) * (remote_size - rest_offset)) - elapsed_time
                                        human_readable_eta = time.strftime("%H:%M:%S", time.gmtime(max(0, eta)))
                                    else:
                                        human_readable_eta = "N/A"
                                    
                                    status = f"Downloaded {human_readable_size(downloaded)}/{human_readable_size(remote_size)} ({percentage:.2f}%) ETA: {human_readable_eta}"
                                    print(f"{status}{' ' * 20}", end="\r", flush=True)
                        
                        # Check size after download loop
                        if os.path.getsize(temp_path) == remote_size:
                            break # Success
                        else:
                            logger.warning(f"Size mismatch after download for {remote_path}. Retrying...")
                            attempt += 1

                except (ftputil.error.FTPIOError, socket.timeout) as e:
                    attempt += 1
                    if attempt <= max_retries:
                        logger.warning(f"Error downloading {remote_path}: {e}. Retrying in 5s... ({attempt}/{max_retries})")
                        time.sleep(5)
                    else:
                        logger.error(f"Failed to download {remote_path} after {max_retries} retries: {e}")
                        return FileTransferResult.FAILED
                except Exception as e:
                    logger.error(f"Unexpected error downloading {remote_path}: {e}")
                    return FileTransferResult.FAILED

            # Clear progress line
            print(f"{' ' * 80}", end="\r", flush=True)

            # Final verification of temp file
            if os.path.getsize(temp_path) != remote_size:
                logger.error(f"Final size verification failed for {temp_path}. Expected {remote_size}, got {os.path.getsize(temp_path)}")
                return FileTransferResult.FAILED

            # Move to final location
            local_dir_path = os.path.dirname(local_path)
            if not os.path.exists(local_dir_path):
                os.makedirs(local_dir_path)
                set_permissions_and_group(local_dir_path)

            os.rename(temp_path, local_path)
            
            # Post-move size verification
            if os.path.getsize(local_path) != remote_size:
                logger.error(f"Post-move verification failed for {local_path}")
                return FileTransferResult.FAILED

            # Set permissions
            if config["folders"]["permissions"]["change_permissions"]:
                set_permissions_and_group(local_path)
            
            return FileTransferResult.DOWNLOADED

        except Exception as e:
            logger.error(f"Error in download_ftp_file for {remote_path}: {e}")
            return FileTransferResult.FAILED
    else:
        # File exists, but let's verify its size if possible
        logger.debug(f"Already exists: {local_path}")
        try:
            remote_size = ftp_host.path.getsize(remote_path)
            local_size = os.path.getsize(local_path)
            if local_size == remote_size:
                logger.info(f"\t+ {padded_name} [EXISTS: VERIFIED]")
                if config["folders"]["permissions"]["change_permissions"]:
                    set_permissions_and_group(local_path)
                return FileTransferResult.EXISTS
            logger.warning(
                f"\t+ {padded_name} [EXISTS: SIZE MISMATCH "
                f"(Local: {local_size}, Remote: {remote_size})]"
            )
            if overwrite:
                try:
                    os.remove(local_path)
                except OSError as e:
                    logger.error(f"Could not remove {local_path} for overwrite: {e}")
                    return FileTransferResult.FAILED
                return download_ftp_file(
                    ftp_host, remote_path, local_path, temp_path, overwrite=True
                )
            return FileTransferResult.FAILED
        except (OSError, ftputil.error.FTPIOError) as e:
            logger.warning(
                f"Could not verify existing file {local_path} against remote: {e}"
            )
            return FileTransferResult.FAILED


def mirror_ftp_directory(
    host, user, password, remote_dir, local_dir, temp_dir, overwrite=False
):
    """
    Mirror an FTP directory tree to a local directory via a temporary directory.

    Args:
        host (str): FTP host.
        user (str): FTP username.
        password (str): FTP password.
        remote_dir (str): Remote directory path.
        local_dir (str): Local directory path.
        temp_dir (str): Temporary directory path.
        overwrite (bool): Whether to overwrite existing files.

    Returns:
        MirrorStats: Aggregated transfer results for the tree.
    """
    encoding = get_ftp_encoding()
    remote_dir = ensure_unicode_path(remote_dir, encoding)
    local_dir = ensure_unicode_path(local_dir, encoding)
    temp_dir = ensure_unicode_path(temp_dir, encoding)
    stats = MirrorStats()

    with ftputil.FTPHost(host, user, password, encoding=encoding) as ftp_host:
        def download_ftp_tree(ftp_host, remote_dir, local_dir, temp_dir):
            try:
                ftp_host.listdir(remote_dir)
                logger.debug(f"Scanning directory {remote_dir}")
                is_directory = True
            except ftputil.error.PermanentError:
                is_directory = False

            if is_directory:
                for item in ftp_host.listdir(remote_dir):
                    item = ensure_unicode_path(item, encoding)
                    remote_path = ftp_host.path.join(remote_dir, item)
                    local_path = os.path.join(local_dir, item)
                    temp_path = os.path.join(temp_dir, item)

                    if ftp_host.path.isdir(remote_path):
                        os.makedirs(local_path, exist_ok=True)
                        os.makedirs(temp_path, exist_ok=True)
                        download_ftp_tree(
                            ftp_host, remote_path, local_path, temp_path
                        )
                    else:
                        stats.record(
                            download_ftp_file(
                                ftp_host, remote_path, local_path, temp_path, overwrite
                            )
                        )
            else:
                local_path = os.path.join(local_dir, os.path.basename(remote_dir))
                temp_path = os.path.join(temp_dir, os.path.basename(remote_dir))
                stats.record(
                    download_ftp_file(
                        ftp_host, remote_dir, local_path, temp_path, overwrite
                    )
                )

            return stats

        return download_ftp_tree(ftp_host, remote_dir, local_dir, temp_dir)


def syncer_download(source, destination):
    """
    Download files from the source to the destination using FTP.

    Args:
        source (str): Source directory path.
        destination (str): Destination directory path.

    Returns:
        MirrorStats: Aggregated transfer results.
    """
    ftp_config = config["ftp"]
    temp_dir = config["folders"]["temp"]
    stats = mirror_ftp_directory(
        ftp_config["host"],
        ftp_config["user"],
        ftp_config["pass"],
        source,
        destination,
        temp_dir,
    )
    if stats.success and os.path.exists(destination):
        set_permissions_and_group(destination)
    return stats


def print_progress_bar(
    current, max, prefix="", suffix="", decimals=1, length=100, fill="█", print_end="\r"
):
    """
    Call in a loop to create a terminal progress bar.

    Args:
        current (int): Current position.
        max (int): Maximum position.
        prefix (str): Prefix string.
        suffix (str): Suffix string.
        decimals (int): Number of decimals in percent complete.
        length (int): Character length of the bar.
        fill (str): Bar fill character.
        print_end (str): End character (e.g., "\r", "\r\n").

    Returns:
        None
    """
    if max <= 0:
        return
    percent = ("{0:." + str(decimals) + "f}").format(100 * current / max)
    filled_length = int(length * current // max)
    bar = fill * filled_length + "-" * (length - filled_length)

    sys.stdout.write(f"\r{prefix} |{bar}| {percent}% {suffix}"), sys.stdout.flush()

    # if current == max:
    #     print_end = '\n'  # Start a new line after the bar is filled

    sys.stdout.write(print_end)


def set_permissions_and_group(path):
    """
    Set permissions and group for a given path.

    Args:
        path (str): Path to the file or directory.

    Returns:
        None
    """
    folder_perms = int(config["folders"]["permissions"]["folders"], 8)
    file_perms = int(config["folders"]["permissions"]["files"], 8)
    group = config["folders"]["permissions"]["group"]

    try:
        gid = grp.getgrnam(group).gr_gid
    except KeyError:
        logger.warning(f"Warning: Group '{group}' not found. Using current group.")
        gid = os.getgid()

    if os.path.isdir(path):

        def set_recursive_permissions_and_group(path):
            if os.path.isdir(path):
                os.chmod(path, folder_perms)
                os.chown(path, -1, gid)
                logger.debug(
                    f"[PERMS DIR] Set permissions {oct(folder_perms)} and group {group} for {path}"
                )
                for entry in os.listdir(path):
                    entry_path = os.path.join(path, entry)
                    set_recursive_permissions_and_group(entry_path)
            else:
                os.chmod(path, file_perms)
                os.chown(path, -1, gid)
                logger.debug(
                    f"[PERMS FILE] Set permissions {oct(file_perms)} and group {group} for {path}"
                )

        set_recursive_permissions_and_group(path)
    else:
        os.chmod(path, file_perms)
        os.chown(path, -1, gid)
        logger.debug(
            f"[PERMS FILE] Set permissions {oct(file_perms)} and group {group} for {path}"
        )


def main():
    """
    Main function to handle the download and synchronization process.

    Returns:
        None
    """
    global config
    global logger

    global args
    global pid_file

    if not args.allow_multiple_instances:
        if os.path.exists(pid_file):
            with open(pid_file, "r") as f:
                old_pid = f.read().strip()
            if old_pid and os.path.exists(f"/proc/{old_pid}"):
                print(f"Script is already running with PID {old_pid}. Exiting.")
                sys.exit(1)

        with open(pid_file, "w") as f:
            f.write(str(os.getpid()))
    while True:
        if os.path.exists(args.config):
            config = load_config(args.config)
            logger = initialize_logger()
        else:
            logger = initialize_logger()
            logger.critical(f"Configuration file {args.config} does not exist.")
            sys.exit(1)

        if args.skip_extensions:
            skip_extensions_arg = args.skip_extensions.split(",")
            config["rules"]["skip_extensions"] = skip_extensions_arg
            logger.info(f"Skip extensions updated to: {skip_extensions_arg}")

        if args.min_file_size:
            config["rules"]["min_file_size"] = args.min_file_size
            logger.info(f"Minimum file size updated to: {args.min_file_size}")

        if args.max_file_size:
            config["rules"]["max_file_size"] = args.max_file_size
            logger.info(f"Maximum file size updated to: {args.max_file_size}")

        if args.skip_regex:
            skip_regex_arg = args.skip_regex.split(",")
            config["rules"]["skip_regex"] = skip_regex_arg
            logger.info(f"Skip regex patterns updated to: {skip_regex_arg}")

        if "timeout" in config["ftp"]:
            socket.setdefaulttimeout(config["ftp"]["timeout"])
            logger.debug(f"Socket timeout set to: {config['ftp']['timeout']}")

        completed_label = config["folders"]["completed"]["label"]
        change_label = config["folders"]["completed"].get("change_label", True)

        if args.debug:
            logger.setLevel(colorlog.DEBUG)

        # Get the label mapping from the config
        label_mapping = config["folders"]["label_mapping"]

        rtorrent_config = config["rtorrent"]
        # labels = config['folders']['labels']

        # Create an object to represent our server. Use the login information in the XMLRPC Login Details section here.
        server_url = build_rtorrent_server_url(rtorrent_config)
        logger.info(
            "Connecting to {}".format(
                server_url.replace(rtorrent_config["pass"], "***")
            )
        )

        allow_xmlrpc_cache = config["rtorrent"].get("allow_xmlrpc_cache", False)
        cache_file = "torrents_cache.pkl"
        server = None
        rpc_failed = False
        torrents = []

        if allow_xmlrpc_cache:
            try:
                with open(cache_file, "rb") as f:
                    torrents = pickle.load(f)
                logger.warning(f"Loaded {len(torrents)} torrents from cache")
            except (FileNotFoundError, EOFError):
                torrents = []

        if len(torrents) == 0:
            try:
                server = connect_rtorrent_server(rtorrent_config)
                mainview = server.download_list("", "main")
                logger.info(f"Found {len(mainview)} torrents")

                torrents = []
                for torrent in mainview:
                    print(".", end="", flush=True)
                    torrents.append(
                        {
                            "id": torrent,
                            "name": ensure_unicode_path(server.d.name(torrent)),
                            "label": ensure_unicode_path(server.d.custom1(torrent)),
                            "is_completed": server.d.complete(torrent),
                            "directory": ensure_unicode_path(
                                server.d.directory(torrent)
                            ),
                            "hash": server.d.hash(torrent),
                        }
                    )

                if allow_xmlrpc_cache:
                    with open(cache_file, "wb") as f:
                        pickle.dump(torrents, f)
                    logger.info(f"Saved {len(torrents)} torrents to cache")
                print("\r" + " " * 100, end="\r")
            except Exception as e:
                rpc_failed = True
                torrents = []
                logger.error(
                    f"An error occurred while connecting to the XMLRPC server: {e}"
                )

        if rpc_failed:
            recheck_time = config["rtorrent"].get("recheck_time", 120)
            logger.warning(f"Skipping this cycle; retrying in {recheck_time}s")
            if args.one_shot:
                break
            time.sleep(recheck_time)
            continue

        # Sort torrents by label
        torrents.sort(key=lambda x: x["label"])
        # Sort torrents by label and priority. If no priority is set, it will be sorted by label. If there is no label found then it will be sorted by name.
        torrents.sort(
            key=lambda x: (
                -label_mapping.get(x["label"], {})
                .get("options", {})
                .get("priority", 99999),
                x["label"],
                x["name"],
            )
        )
        for torrent_dict in torrents:
            if torrent_dict["is_completed"]:
                logger.info(f"[{torrent_dict['label']:<15}] {torrent_dict['name']}")
            else:
                logger.warning(f"[{torrent_dict['label']:<14}*] {torrent_dict['name']}")

        for torrent_dict in torrents:
            if torrent_dict["label"] != completed_label:
                # Check if the label exists in the label mapping
                if torrent_dict["label"] in label_mapping:
                    if torrent_dict["is_completed"]:
                        logger.info("-" * 100)
                        # Get the destination path from the label mapping
                        destination_path = label_mapping[torrent_dict["label"]]["path"]
                        root_dir = config["folders"]["root"]
                        if not os.path.exists(root_dir):
                            logger.critical(f"No root directory: {root_dir}")
                            exit(1)
                        destination = os.path.join(
                            root_dir, destination_path, torrent_dict["name"]
                        )
                        logger.debug(f"Destination: {destination}")
                        logger.info(
                            f"=> {torrent_dict['name']} ({torrent_dict['label']})"
                        )
                        source_directory = resolve_torrent_source_directory(
                            torrent_dict["directory"], torrent_dict["name"]
                        )

                        if not args.dry_run:
                            try:
                                mirror_stats = syncer_download(
                                    source_directory, destination
                                )

                                if not mirror_stats.success:
                                    logger.error(
                                        f"Download failed or incomplete for "
                                        f"{torrent_dict['name']}, skipping label update "
                                        f"and actions"
                                    )
                                elif not mirror_stats.has_content:
                                    logger.warning(
                                        f"No files transferred for {torrent_dict['name']} "
                                        f"(downloaded={mirror_stats.downloaded}, "
                                        f"exists={mirror_stats.exists}, "
                                        f"skipped={mirror_stats.skipped}), "
                                        f"skipping label update and actions"
                                    )
                                else:
                                    message = (
                                        f"[{torrent_dict['label']}] {torrent_dict['name']}"
                                    )
                                    send_telegram_notification(config, message)

                                    if change_label and not args.dont_change_label:
                                        try:
                                            if server is None:
                                                server = connect_rtorrent_server(
                                                    rtorrent_config
                                                )
                                            logger.info(
                                                f"\t= Setting label on "
                                                f"{torrent_dict['name']}"
                                            )
                                            server.d.custom1.set(
                                                torrent_dict["id"], completed_label
                                            )
                                        except Exception as e:
                                            logger.error(
                                                f"\t= Failed to set label on "
                                                f"{torrent_dict['name']}: {e}"
                                            )
                                    else:
                                        logger.warning("\t= Skipping setting label")

                                    if "actions" in label_mapping[torrent_dict["label"]]:
                                        for action in label_mapping[torrent_dict["label"]][
                                            "actions"
                                        ]:
                                            logger.info(f"\t= Executing action: {action}")
                                            if action["name"] == "notify_radarr":
                                                radarr_import_base_path = action[
                                                    "radarr_import_base_path"
                                                ]
                                                radarr = RadarrAPI(
                                                    config["radarr"]["baseurl"],
                                                    config["radarr"]["api_key"],
                                                )
                                                radarr_import_full_path = f"{radarr_import_base_path}/{torrent_dict['name']}/"
                                                logger.debug(
                                                    f"\t\t= Importing {radarr_import_full_path}"
                                                )
                                                radarr.send_command(
                                                    "DownloadedMoviesScan",
                                                    path=radarr_import_full_path,
                                                )
                                            elif action["name"] == "notify_sonarr":
                                                sonarr_import_base_path = action[
                                                    "sonarr_import_base_path"
                                                ]
                                                sonarr = SonarrAPI(
                                                    config["sonarr"]["baseurl"],
                                                    config["sonarr"]["api_key"],
                                                )
                                                sonarr_import_full_path = f"{sonarr_import_base_path}/{torrent_dict['name']}/"
                                                logger.debug(
                                                    f"\t\t= Importing {sonarr_import_full_path}"
                                                )
                                                sonarr.send_command(
                                                    "DownloadedEpisodesScan",
                                                    path=sonarr_import_full_path,
                                                )
                            except Exception as e:
                                logger.error(f"Error processing torrent {torrent_dict['name']}: {e}")

        if args.one_shot:
            break
        else:
            recheck_time = config["rtorrent"].get("recheck_time", 120)
            print("\n")
            current_pos = 0
            while current_pos < recheck_time:
                current_pos += 1
                print_progress_bar(
                    current_pos,
                    recheck_time,
                    prefix=f"Wait {recheck_time - current_pos:>3}s for next check",
                    suffix="",
                    length=80,
                )
                time.sleep(1)
            print("\r" + " " * 100, end="\r")  # Clear the line
            print("\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Simulate the download process without actually downloading or changing labels",
    )
    parser.add_argument(
        "--one-shot",
        action="store_true",
        help="Run the script only once without looping",
    )
    parser.add_argument("--debug", action="store_true", help="Enable debug mode")
    parser.add_argument(
        "--config", type=str, default="config.yaml", help="Path to the config file"
    )
    parser.add_argument(
        "--skip-extensions",
        type=str,
        help="Comma-separated list of file extensions to skip",
    )
    parser.add_argument(
        "--dont-change-label",
        action="store_true",
        help="Don't change the label of the torrents when download completes",
    )
    parser.add_argument(
        "--min-file-size", type=int, help="Minimum file size to download (in bytes)"
    )
    parser.add_argument(
        "--max-file-size", type=int, help="Maximum file size to download (in bytes)"
    )
    parser.add_argument(
        "--skip-regex", type=str, help="Comma-separated list of regex patterns to skip"
    )
    parser.add_argument(
        "--allow-multiple-instances",
        action="store_true",
        default=False,
        help="Allow multiple instances of the script to run",
    )
    parser.add_argument(
        "--pid-file",
        type=str,
        default="/tmp/downloadarr.pid",
        help="Path to the PID file",
    )
    args = parser.parse_args()

    pid_file = args.pid_file

    try:
        main()
    finally:
        if not args.allow_multiple_instances and os.path.exists(pid_file):
            os.remove(pid_file)
