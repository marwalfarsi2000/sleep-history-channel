from unittest.mock import patch, MagicMock
import git_sync


def test_git_sync_success():
    with patch("subprocess.run") as mock_run:
        mock_diff = MagicMock()
        mock_diff.returncode = 1
        mock_ok = MagicMock()
        mock_ok.returncode = 0
        mock_run.side_effect = [mock_ok, mock_ok, mock_ok, mock_diff, mock_ok, mock_ok, mock_ok]

        res = git_sync.sync_git_catalogue()
        assert res is True


def test_git_sync_push_failure_is_non_fatal():
    with patch("subprocess.run") as mock_run:
        mock_diff = MagicMock()
        mock_diff.returncode = 1
        mock_ok = MagicMock()
        mock_ok.returncode = 0
        mock_fail = MagicMock()
        mock_fail.returncode = 1
        mock_run.side_effect = [mock_ok, mock_ok, mock_ok, mock_diff, mock_ok, mock_ok, mock_fail, mock_ok, mock_fail, mock_ok, mock_fail]

        res = git_sync.sync_git_catalogue()
        assert res is False
