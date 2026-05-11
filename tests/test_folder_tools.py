"""Unit tests for plaud-mcp folder tools (cmd_875 wave2)."""
from __future__ import annotations

from unittest.mock import patch

import pytest

from plaud_mcp.server import (
    create_folder,
    list_folders,
    move_file_to_folder,
    unset_file_folder,
)


@pytest.fixture
def mock_client():
    """Return a mock PlaudClient context manager."""
    with patch("plaud_mcp.server.PlaudClient") as mock_client_cls:
        instance = mock_client_cls.return_value.__aenter__.return_value
        yield instance


@pytest.mark.asyncio
async def test_list_folders(mock_client):
    mock_client.get.return_value = {
        "status": 0,
        "data_filetag_total": 6,
        "data_filetag_list": [
            {"id": "abc123", "name": "Archive", "icon": None, "color": "#191919"},
            {"id": "def456", "name": "YMC", "icon": "e627", "color": "#4c8eff"},
        ],
    }
    result = await list_folders()
    assert result["total"] == 6
    assert len(result["folders"]) == 2
    assert result["folders"][0]["name"] == "Archive"
    mock_client.get.assert_called_once_with("/filetag/")


@pytest.mark.asyncio
async def test_create_folder_dry_run_no_duplicate(mock_client):
    mock_client.get.return_value = {
        "data_filetag_list": [
            {"id": "existing", "name": "Archive"},
        ],
    }
    result = await create_folder(name="BNI", color="#fb5c5c", dry_run=True)
    assert result["ok"] is True
    assert result["dry_run"] is True
    assert result["duplicate_found"] is False
    assert result["name"] == "BNI"
    assert result["color"] == "#fb5c5c"
    mock_client.post.assert_not_called()


@pytest.mark.asyncio
async def test_create_folder_duplicate(mock_client):
    mock_client.get.return_value = {
        "data_filetag_list": [
            {"id": "dup-id-123", "name": "BNI"},
        ],
    }
    result = await create_folder(name="BNI", dry_run=True)
    assert result["ok"] is True
    assert result["duplicate_found"] is True
    assert result["existing_folder_id"] == "dup-id-123"
    mock_client.post.assert_not_called()


@pytest.mark.asyncio
async def test_create_folder_empty_name_rejected(mock_client):
    result = await create_folder(name="   ", dry_run=True)
    assert result["ok"] is False
    assert result["errors"] == "empty name"
    mock_client.get.assert_not_called()
    mock_client.post.assert_not_called()


@pytest.mark.asyncio
async def test_create_folder_live(mock_client):
    mock_client.get.return_value = {"data_filetag_list": []}
    mock_client.post.return_value = {
        "status": 0,
        "data_filetag": {
            "id": "new-folder-id",
            "name": "TestFolder",
            "icon": None,
            "color": "#123456",
        },
    }
    result = await create_folder(name="TestFolder", color="#123456", dry_run=False)
    assert result["ok"] is True
    assert result["dry_run"] is False
    assert result["new_folder"]["id"] == "new-folder-id"
    mock_client.post.assert_called_once_with("/filetag/", json={"name": "TestFolder", "color": "#123456"})


@pytest.mark.asyncio
async def test_move_file_to_folder_dry_run(mock_client):
    mock_client.get.return_value = {
        "data": {
            "filetag_id_list": ["old-folder-id"],
        },
    }
    result = await move_file_to_folder(
        file_id="file-abc",
        folder_id="target-folder",
        dry_run=True,
    )
    assert result["ok"] is True
    assert result["dry_run"] is True
    assert result["previous_filetag_id_list"] == ["old-folder-id"]
    assert result["new_filetag_id_list"] == ["target-folder"]
    mock_client.patch.assert_not_called()


@pytest.mark.asyncio
async def test_move_file_to_folder_already_in_folder(mock_client):
    mock_client.get.return_value = {
        "data": {
            "filetag_id_list": ["target-folder"],
        },
    }
    result = await move_file_to_folder(
        file_id="file-abc",
        folder_id="target-folder",
        dry_run=True,
    )
    assert result["ok"] is True
    assert result["message"] == "File is already in the target folder"
    mock_client.patch.assert_not_called()


@pytest.mark.asyncio
async def test_move_file_to_folder_empty_file_id(mock_client):
    result = await move_file_to_folder(file_id="", folder_id="folder-1")
    assert result["ok"] is False
    assert result["errors"] == "empty file_id"


@pytest.mark.asyncio
async def test_unset_file_folder_dry_run(mock_client):
    mock_client.get.return_value = {
        "data": {
            "filetag_id_list": ["folder-a", "folder-b"],
        },
    }
    result = await unset_file_folder(file_id="file-xyz", dry_run=True)
    assert result["ok"] is True
    assert result["dry_run"] is True
    assert result["previous_filetag_id_list"] == ["folder-a", "folder-b"]
    assert result["new_filetag_id_list"] == []
    mock_client.patch.assert_not_called()


@pytest.mark.asyncio
async def test_unset_file_folder_not_in_any_folder(mock_client):
    mock_client.get.return_value = {
        "data": {
            "filetag_id_list": [],
        },
    }
    result = await unset_file_folder(file_id="file-xyz", dry_run=True)
    assert result["ok"] is True
    assert result["message"] == "File is not in any folder"
    mock_client.patch.assert_not_called()


@pytest.mark.asyncio
async def test_unset_file_folder_empty_file_id(mock_client):
    result = await unset_file_folder(file_id="  ")
    assert result["ok"] is False
    assert result["errors"] == "empty file_id"
