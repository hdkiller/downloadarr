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


def load_config(file_path):
    """
    Load the YAML configuration file.

    Args:
        file_path (str): Path to the configuration file.

    Returns:
        dict: Configuration data.
    """
    with open(file_path, "r") as file:
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



# sonarr = SonarrAPI(config["sonarr"]["baseurl"], config["sonarr"]["api_key"])
# pprint(sonarr.send_command("DownloadedEpisodesScan",path="/media/Public/Downloads/Shows/"))
# pprint(sonarr.all_commands())



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


def download_ftp_file(ftp_host, remote_path, local_path, temp_path, overwrite=False):
    """
    Download a file from the FTP server to a temporary location, then move it to the final destination.

    Args:
        ftp_host (ftputil.FTPHost): FTP host object.
        remote_path (str): Path to the remote file.
        local_path (str): Path to the local file.
        temp_path (str): Path to the temporary file.
        overwrite (bool): Whether to overwrite the existing file.

    Returns:
        None
    """
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

        with ftp_host.open(remote_path, "rb") as remote_file:
            file_size = ftp_host.path.getsize(remote_path)

            # Check if the file size is too big
            if file_size > config["rules"]["max_file_size"]:
                logger.warning(f"\t- {padded_name} [SKIPPED: too big]")
                return
            
            # Check if the file size is too small
            if file_size < config["rules"]["min_file_size"]:
                logger.warning(f"\t- {padded_name} [SKIPPED: too small]")
                return
            
            # Check if the file name matches any of the skip regex patterns
            if any(
                re.match(pattern, remote_path)
                for pattern in config["rules"]["skip_regex"]
            ):
                logger.warning(f"\t- {padded_name} [SKIPPED: regex]")
                return
            
            # Check if the file extension is in the skip list
            if any(
                remote_path.endswith(ext) for ext in config["rules"]["skip_extensions"]
            ):
                logger.warning(f"\t- {padded_name} [SKIPPED: extension]")
                return

            logger.info(f"\t+ {padded_name} [OK]")

            start_time = time.time()
            with open(temp_path, "wb") as local_file:
                downloaded = 0
                block_size = 8192
                while True:
                    buffer = remote_file.read(block_size)
                    if not buffer:
                        break
                    downloaded += len(buffer)
                    local_file.write(buffer)
                    human_readable_downloaded = human_readable_size(downloaded)
                    human_readable_file_size = human_readable_size(file_size)
                    percentage = (downloaded / file_size) * 100

                    elapsed_time = time.time() - start_time
                    if downloaded > 0:
                        estimated_total_time = (elapsed_time / downloaded) * file_size
                        eta = estimated_total_time - elapsed_time
                        human_readable_eta = time.strftime("%H:%M:%S", time.gmtime(eta))
                    else:
                        human_readable_eta = "N/A"

                    status = f"Downloaded {human_readable_downloaded}/{human_readable_file_size} ({percentage:.2f}%) ETA: {human_readable_eta}"
                    print(f"{status}{' ' * 20}", end="\r", flush=True)
            # print(f"Rename {temp_path} to {local_path}")
            print(f"{' ' * 60}", end="\r", flush=True)
            # Move the file from temp to final location
            local_dir_path = os.path.dirname(local_path)
            if not os.path.exists(local_dir_path):
                os.makedirs(local_dir_path)
                logger.debug(f"Created directory: {local_dir_path}")
                set_permissions_and_group(local_dir_path)

            os.rename(temp_path, local_path)
            logger.debug(f"Moved to: {local_path}")

            # Set permissions and group
            if config["folders"]["permissions"]["change_permissions"]:
                logger.debug(f"Setting permissions and group for {local_path}")
                set_permissions_and_group(local_path)
            else:
                logger.debug(f"Skipping setting permissions and group for {local_path}")
    else:
        logger.debug(f"Already exists: {local_path}")
        set_permissions_and_group(local_path)
        logger.info(f"\t+ {padded_name} [EXISTS]")


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
        None
    """
    with ftputil.FTPHost(host, user, password) as ftp_host:

        def download_ftp_tree(ftp_host, remote_dir, local_dir, temp_dir):
            try:
                dirs = ftp_host.listdir(remote_dir)
                logger.debug(f"Found {len(dirs)} items in {remote_dir}")
                is_directory = True
            except ftputil.error.PermanentError:
                is_directory = False

            if is_directory:
                for item in ftp_host.listdir(remote_dir):
                    remote_path = ftp_host.path.join(remote_dir, item)
                    local_path = os.path.join(local_dir, item)
                    temp_path = os.path.join(temp_dir, item)

                    if ftp_host.path.isdir(remote_path):
                        if not os.path.exists(local_path):
                            os.makedirs(local_path)
                        if not os.path.exists(temp_path):
                            os.makedirs(temp_path)
                        download_ftp_tree(ftp_host, remote_path, local_dir, temp_dir)
                    else:
                        download_ftp_file(
                            ftp_host, remote_path, local_path, temp_path, overwrite
                        )
            else:
                # print(f"File: {remote_dir}")
                local_path = os.path.join(local_dir, os.path.basename(remote_dir))
                temp_path = os.path.join(temp_dir, os.path.basename(remote_dir))
                download_ftp_file(
                    ftp_host, remote_dir, local_path, temp_path, overwrite
                )

        download_ftp_tree(ftp_host, remote_dir, local_dir, temp_dir)


def syncer_download(source, destination):
    """
    Download files from the source to the destination using FTP.

    Args:
        source (str): Source directory path.
        destination (str): Destination directory path.

    Returns:
        None
    """
    ftp_config = load_config("config.yaml")["ftp"]
    temp_dir = load_config("config.yaml")["folders"]["temp"]
    mirror_ftp_directory(
        ftp_config["host"],
        ftp_config["user"],
        ftp_config["pass"],
        source,
        destination,
        temp_dir,
    )
    set_permissions_and_group(destination)


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
    parser.add_argument("--config", type=str, default="config.yaml", help="Path to the config file")
    parser.add_argument("--skip-extensions", type=str, help="Comma-separated list of file extensions to skip")
    parser.add_argument("--dont-change-label", action="store_true", help="Don't change the label of the torrents when download completes")
    parser.add_argument("--min-file-size", type=int, help="Minimum file size to download (in bytes)")
    parser.add_argument("--max-file-size", type=int, help="Maximum file size to download (in bytes)")
    parser.add_argument("--skip-regex", type=str, help="Comma-separated list of regex patterns to skip")
    args = parser.parse_args()
    while True:
        if os.path.exists(args.config):
            config = load_config(args.config)
            logger = initialize_logger()
        else:
            logger = initialize_logger()
            logger.critical(f"Configuration file {args.config} does not exist.")
            sys.exit(1)

        if args.skip_extensions:
            skip_extensions_arg = args.skip_extensions.split(',')
            config["rules"]["skip_extensions"] = skip_extensions_arg
            logger.info(f"Skip extensions updated to: {skip_extensions_arg}")

        if args.min_file_size:
            config["rules"]["min_file_size"] = args.min_file_size
            logger.info(f"Minimum file size updated to: {args.min_file_size}")

        if args.max_file_size:
            config["rules"]["max_file_size"] = args.max_file_size
            logger.info(f"Maximum file size updated to: {args.max_file_size}")

        if args.skip_regex:
            skip_regex_arg = args.skip_regex.split(',')
            config["rules"]["skip_regex"] = skip_regex_arg
            logger.info(f"Skip regex patterns updated to: {skip_regex_arg}")

        completed_label = config["folders"]["completed"]["label"]
        change_label = config["folders"]["completed"].get("change_label", True)
        
        if args.debug:
            logger.setLevel(colorlog.DEBUG)

        # Get the label mapping from the config
        label_mapping = config["folders"]["label_mapping"]

        rtorrent_config = config["rtorrent"]
        # labels = config['folders']['labels']

        # Create an object to represent our server. Use the login information in the XMLRPC Login Details section here.
        server_url = f"https://{rtorrent_config['user']}:{rtorrent_config['pass']}@{rtorrent_config['host']}:{rtorrent_config['port']}{rtorrent_config['path']}"

        logger.info(
            "Connecting to {}".format(
                server_url.replace(rtorrent_config["pass"], "***")
            )
        )

        allow_xmlrpc_cache = config["rtorrent"].get("allow_xmlrpc_cache", False)

        cache_file = "torrents_cache.pkl"

        if allow_xmlrpc_cache:
            try:
                with open(cache_file, "rb") as f:
                    torrents = pickle.load(f)
                logger.warning(f"Loaded {len(torrents)} torrents from cache")
            except (FileNotFoundError, EOFError):
                torrents = []
        else:
            torrents = []

        if len(torrents) == 0:
            try:
                server = xmlrpc.client.Server(server_url)
                mainview = server.download_list("", "main")
                logger.info(f"Found {len(mainview)} torrents")

                torrents = []

                for torrent in mainview:
                    torrent_dict = {}
                    # print(f"Processing {torrent}")
                    print(".", end="", flush=True)
                    torrent_dict["id"] = torrent
                    torrent_dict["name"] = server.d.name(torrent)
                    torrent_dict["label"] = server.d.custom1(torrent)
                    torrent_dict["is_completed"] = server.d.complete(torrent)
                    torrent_dict["directory"] = server.d.directory(torrent)
                    torrent_dict["hash"] = server.d.hash(torrent)

                    torrents.append(torrent_dict)

                if allow_xmlrpc_cache:
                    with open(cache_file, "wb") as f:
                        pickle.dump(torrents, f)
                    logger.info(f"Saved {len(torrents)} torrents to cache")
                print("\r" + " " * 100, end="\r")  # Clear the line
            except Exception as e:
                logger.error(
                    f"An error occurred while connecting to the XMLRPC server: {e}"
                )

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
                        is_dir = torrent_dict["name"] in torrent_dict["directory"]
                        if is_dir is False:
                            source_directory = (
                                torrent_dict["directory"] + "/" + torrent_dict["name"]
                            )
                        else:
                            source_directory = torrent_dict["directory"]

                        # logger.info(f"{source_directory} => {destination}")
                        if not args.dry_run:
                            syncer_download(source_directory, destination)

                            if change_label and not args.dont_change_label:
                                if "server" in locals():
                                    logger.info(
                                        f"\t= Setting label on {torrent_dict['name']}"
                                    )
                                    server.d.custom1.set(
                                        torrent_dict["id"], completed_label
                                    )
                                else:
                                    logger.error(
                                        "\t= Cannot set label when caching is on"
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
                                        # pprint(radarr.all_commands())

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
    main()
