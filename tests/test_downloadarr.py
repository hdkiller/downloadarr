"""Unit tests for downloadarr helpers and transfer logic."""

import os
import sys
import tempfile
from unittest.mock import MagicMock, patch

import pytest

# downloadarr expects globals when FTP helpers run; tests only use pure functions.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import downloadarr as da


@pytest.fixture
def sample_config():
    return {
        "ftp": {"encoding": "utf-8", "retries": 1},
        "folders": {
            "temp": "/tmp/incomplete",
            "permissions": {
                "change_permissions": False,
                "folders": "0775",
                "files": "0664",
                "group": "users",
            },
        },
        "rules": {
            "max_file_size": 100 * 1024 * 1024,
            "min_file_size": 1024,
            "skip_regex": [r".*\.sample.*"],
            "skip_extensions": [".nfo", ".txt"],
        },
    }


class TestEnsureUnicodePath:
    def test_preserves_accented_unicode(self):
        path = "Films/Café ősz/töltés.mkv"
        assert da.ensure_unicode_path(path) == path

    def test_decodes_utf8_bytes(self):
        raw = "Műsor".encode("utf-8")
        assert da.ensure_unicode_path(raw) == "Műsor"

    def test_normalizes_to_nfc(self):
        decomposed = "e\u0301"  # e + combining acute
        assert da.ensure_unicode_path(decomposed) == "\u00e9"


class TestResolveTorrentSourceDirectory:
    def test_multi_file_torrent_uses_directory(self):
        assert (
            da.resolve_torrent_source_directory("/data/downloads/My.Show.S01", "My.Show.S01")
            == "/data/downloads/My.Show.S01"
        )

    def test_single_file_appends_name(self):
        assert (
            da.resolve_torrent_source_directory("/data/downloads", "movie.mkv")
            == "/data/downloads/movie.mkv"
        )

    def test_unicode_paths(self):
        directory = "/data/Letöltések"
        name = "Állatok ősszel"
        assert da.resolve_torrent_source_directory(directory, name) == f"{directory}/{name}"


class TestHumanReadableSize:
    def test_bytes(self):
        assert da.human_readable_size(512) == "512.00 B"

    def test_large_value_returns_string(self):
        result = da.human_readable_size(1024**6)
        assert result.endswith("PB")


class TestMirrorStats:
    def test_success_when_no_failures(self):
        stats = da.MirrorStats(skipped=3)
        assert stats.success is True
        assert stats.has_content is False

    def test_has_content_when_downloaded(self):
        stats = da.MirrorStats(downloaded=1, skipped=2)
        assert stats.has_content is True

    def test_record_increments(self):
        stats = da.MirrorStats()
        stats.record(da.FileTransferResult.DOWNLOADED)
        stats.record(da.FileTransferResult.SKIPPED)
        assert stats.downloaded == 1
        assert stats.skipped == 1


class TestPrintProgressBar:
    def test_zero_max_does_not_raise(self):
        da.print_progress_bar(0, 0)


class TestDownloadFtpFile:
    def test_skip_extension_returns_skipped(self, sample_config):
        da.config = sample_config
        da.logger = MagicMock()

        ftp_host = MagicMock()
        ftp_host.path.getsize.return_value = 5000

        with tempfile.TemporaryDirectory() as tmp:
            local = os.path.join(tmp, "readme.nfo")
            temp = os.path.join(tmp, ".incomplete", "readme.nfo")
            result = da.download_ftp_file(
                ftp_host, "/remote/readme.nfo", local, temp
            )

        assert result == da.FileTransferResult.SKIPPED
        assert not os.path.exists(local)

    def test_unicode_remote_path_uses_str_not_bytes(self, sample_config):
        da.config = sample_config
        da.logger = MagicMock()

        ftp_host = MagicMock()
        ftp_host.path.getsize.return_value = 500  # below min_file_size

        remote = "/remote/Café ősz/film.mkv"
        with tempfile.TemporaryDirectory() as tmp:
            local = os.path.join(tmp, "film.mkv")
            temp = os.path.join(tmp, ".incomplete", "film.mkv")
            result = da.download_ftp_file(ftp_host, remote, local, temp)

        assert result == da.FileTransferResult.SKIPPED
        ftp_host.path.getsize.assert_called_once_with(remote)
        ftp_host.open.assert_not_called()


class TestMirrorFtpDirectory:
    def test_subdirectory_paths_use_child_directories(self, sample_config):
        da.config = sample_config
        da.logger = MagicMock()
        calls = []

        def fake_download_file(ftp_host, remote_path, local_path, temp_path, overwrite=False):
            calls.append((remote_path, local_path))
            return da.FileTransferResult.DOWNLOADED

        with tempfile.TemporaryDirectory() as tmp:
            local_root = os.path.join(tmp, "local")
            temp_root = os.path.join(tmp, "temp")

            with patch.object(da, "download_ftp_file", side_effect=fake_download_file):
                with patch("downloadarr.ftputil.FTPHost") as mock_host_cls:
                    ftp_host = MagicMock()
                    mock_host_cls.return_value.__enter__.return_value = ftp_host

                    def listdir(path):
                        if path.endswith("/torrent"):
                            return ["subs", "movie.mkv"]
                        if path.endswith("/subs"):
                            return ["en.srt"]
                        return []

                    ftp_host.listdir.side_effect = listdir
                    ftp_host.path.join.side_effect = lambda a, b: f"{a.rstrip('/')}/{b}"
                    ftp_host.path.isdir.side_effect = lambda path: path.endswith("/subs")

                    stats = da.mirror_ftp_directory(
                        "host", "user", "pass", "/torrent", local_root, temp_root
                    )

        assert stats.downloaded == 2
        assert any(
            remote.endswith("subs/en.srt") and "subs" in local and local.endswith("en.srt")
            for remote, local in calls
        )


class TestBuildRtorrentServerUrl:
    def test_builds_https_url(self):
        cfg = {
            "user": "u",
            "pass": "p",
            "host": "example.com",
            "port": 443,
            "path": "/rpc",
        }
        assert da.build_rtorrent_server_url(cfg) == "https://u:p@example.com:443/rpc"
