import yaml
from pprint import pprint
import ftputil
import os
import argparse
import curses
import xmlrpc.client
import time
import colorlog

# Configure colorlog
handler = colorlog.StreamHandler()
handler.setFormatter(colorlog.ColoredFormatter(
    '%(log_color)s%(levelname)-8s%(reset)s %(message)s',
    log_colors={
        'DEBUG': 'cyan',
        'INFO': 'green',
        'WARNING': 'yellow',
        'ERROR': 'red',
        'CRITICAL': 'red,bg_white',
    }
))
logger = colorlog.getLogger()
logger.addHandler(handler)
logger.setLevel(colorlog.DEBUG)

DEVELOPMENT = False

def load_config(file_path):
    with open(file_path, 'r') as file:
        return yaml.safe_load(file)
    
def human_readable_size(size, decimal_places=2):
    for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
        if size < 1024.0:
            return f"{size:.{decimal_places}f} {unit}"
        size /= 1024.0
        
def download_ftp_file(ftp_host, remote_path, local_path, temp_path, overwrite=False):
    """
    Downloads a file from the FTP server to a temporary location, then moves it to the final destination.
    """
    if not os.path.exists(local_path) or overwrite:
        logger.info(f"Downloading file {remote_path} to {temp_path}")
        temp_dir_path = os.path.dirname(temp_path)
        if not os.path.exists(temp_dir_path):
            os.makedirs(temp_dir_path)
            logger.info(f"Created directory: {temp_dir_path}")

        with ftp_host.open(remote_path, 'rb') as remote_file:
            file_size = ftp_host.path.getsize(remote_path)
            if DEVELOPMENT and file_size > 100000000:
                logger.warning(f"DEVELOPMENT: Skipping {remote_path} because it is too big")
                return
            start_time = time.time()      
            with open(temp_path, 'wb') as local_file:
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
            logger.info()
            # print(f"Rename {temp_path} to {local_path}")
            # Move the file from temp to final location
            local_dir_path = os.path.dirname(local_path)
            if not os.path.exists(local_dir_path):
                os.makedirs(local_dir_path)
                logger.info(f"Created directory: {local_dir_path}")
            os.rename(temp_path, local_path)
            logger.info(f"Moved to: {local_path}")
    else:
        logger.info(f"Already exists: {local_path}")

def mirror_ftp_directory(host, user, password, remote_dir, local_dir, temp_dir, overwrite=False):
    """
    Mirrors an FTP directory tree to a local directory via a temporary directory.
    """
    with ftputil.FTPHost(host, user, password) as ftp_host:
        def download_ftp_tree(ftp_host, remote_dir, local_dir, temp_dir):
 
            # print(f"Checking if {remote_dir} is a directory")
            try:
                pprint(ftp_host.listdir(remote_dir))
                is_directory = True
            except ftputil.error.PermanentError:
                is_directory = False

            if is_directory:
                # print(f"Directory: {remote_dir}")
                for item in ftp_host.listdir(remote_dir):
                    remote_path = ftp_host.path.join(remote_dir, item)
                    local_path = os.path.join(local_dir, item)
                    temp_path = os.path.join(temp_dir, item)

                    if ftp_host.path.isdir(remote_path):
                        if not os.path.exists(local_path):
                            os.makedirs(local_path)
                        if not os.path.exists(temp_path):
                            os.makedirs(temp_path)
                        download_ftp_tree(ftp_host, remote_path, local_dir, temp_path)
                    else:
                        download_ftp_file(ftp_host, remote_path, local_path, temp_path, overwrite)
            else:
                # print(f"File: {remote_dir}")
                local_path = os.path.join(local_dir, os.path.basename(remote_dir))
                temp_path = os.path.join(temp_dir, os.path.basename(remote_dir))
                download_ftp_file(ftp_host, remote_dir, local_path, temp_path, overwrite)

        download_ftp_tree(ftp_host, remote_dir, local_dir, temp_dir)

def syncer_download(source, destination):
    logger.info(f"Downloading {source} to {destination} via FTPS")
    ftp_config = load_config('config.yaml')['ftp']
    temp_dir = load_config('config.yaml')['folders']['temp']
    mirror_ftp_directory(ftp_config['host'], ftp_config['user'], ftp_config['pass'], source, destination, temp_dir)
    

def main():
    # Parse command-line arguments
    parser = argparse.ArgumentParser()
    parser.add_argument('--dry-run', action='store_true', help='Simulate the download process without actually downloading or changing labels')
    args = parser.parse_args()

    config = load_config('config.yaml')

    completed_label = config['folders']['completed']['label']
    change_label = config['folders']['completed'].get('change_label', True) 

    rtorrent_config = config['rtorrent']
    labels = config['folders']['labels']

    # Create an object to represent our server. Use the login information in the XMLRPC Login Details section here.
    server_url = f"https://{rtorrent_config['user']}:{rtorrent_config['pass']}@{rtorrent_config['host']}:{rtorrent_config['port']}{rtorrent_config['path']}"

    logger.info("Connecting to {}".format(server_url.replace(rtorrent_config['pass'], '***')))

    import pickle
    cache_file = 'torrents_cache.pkl'

    try:
        with open(cache_file, 'rb') as f:
            torrents = pickle.load(f)
        logger.warning(f"Loaded {len(torrents)} torrents from cache")
    except (FileNotFoundError, EOFError):
        try:
            server = xmlrpc.client.Server(server_url)
            mainview = server.download_list("", "main")
            logger.info(f"Found {len(mainview)} torrents")
            
            torrents = []

            for torrent in mainview:
                torrent_dict = {}
                # print(f"Processing {torrent}")
                print(".", end="", flush=True)
                torrent_dict['id'] = torrent
                torrent_dict['name'] = server.d.name(torrent)
                torrent_dict['label'] = server.d.custom1(torrent)
                torrent_dict['is_completed'] = server.d.complete(torrent)
                torrent_dict['directory'] = server.d.directory(torrent)
                torrent_dict['hash'] = server.d.hash(torrent)
                
                torrents.append(torrent_dict)
            
            with open(cache_file, 'wb') as f:
                pickle.dump(torrents, f)
            logger.info(f"Saved {len(torrents)} torrents to cache")
            print("\r" + " " * 100, end="\r")  # Clear the line
        except Exception as e:
            logger.error(f"An error occurred while connecting to the XMLRPC server: {e}")
    
    torrents.sort(key=lambda x: x['label'])

    for torrent_dict in torrents:
        logger.info(f"[{torrent_dict['label']:<15}] {torrent_dict['name']}")

    logger.info("Downloading")
    for torrent_dict in torrents:
        # print(f"[{torrent_dict['label']:<15}] {torrent_dict['name']}")

        if torrent_dict['label'] != completed_label:
            if torrent_dict['label'] in labels:
                if torrent_dict['is_completed']:
                    destination = config['folders']['root'] + labels[torrent_dict['label']] + torrent_dict['name']
                    logger.info(f"[{torrent_dict['label']}] {torrent_dict['name']}")
                    is_dir = torrent_dict['name'] in torrent_dict['directory']
                    if is_dir is False:
                        source_directory = torrent_dict['directory'] + "/" + torrent_dict['name']
                    else:
                        source_directory = torrent_dict['directory']    
                    
                    logger.info(f"{source_directory} => {destination}")
                    logger.info("----------------------------------------")
                    if not args.dry_run:
                        syncer_download(source_directory, destination)
                        if change_label == True:
                            server.d.custom1.set(torrent, "tmp_" + completed_label)
                        else:
                            logger.warning("Skipping setting label")
                    logger.info("----------------------------------------")
if __name__ == "__main__":
    main()
