import os
import tempfile
import subprocess
from pathlib import Path
from unittest.mock import patch, MagicMock, mock_open
import pytest
from uvrepin.main import (
    main, parse_workspace_conflict, WorkspaceConflict, 
    determine_target_versions, align_workspace_members,
    ConflictResolution, is_ci_environment, prompt_user_for_conflict_resolution,
    show_manual_resolution_help, uv_runner
)


class TestWorkspaceConflictParsing:
    def test_parse_workspace_conflict_valid(self):
        stderr = """
No solution found when resolving dependencies:
  Because common[dev] depends on flake8==7.2.0 and qluster-sdk[dev] depends on flake8==7.3.0, we can resolve the conflict.
  Because qluster-sdk[dev] depends on pytest==8.1.0 and common[dev] depends on pytest==8.0.0, we can't proceed.
"""
        conflicts = parse_workspace_conflict(stderr)
        
        assert conflicts is not None
        assert len(conflicts) == 2
        
        # Check first conflict
        assert conflicts[0].package_name == "flake8"
        assert conflicts[0].extra_name == "dev"
        assert conflicts[0].conflicts == {"common": "7.2.0", "qluster-sdk": "7.3.0"}
        
        # Check second conflict
        assert conflicts[1].package_name == "pytest"
        assert conflicts[1].extra_name == "dev"
        assert conflicts[1].conflicts == {"qluster-sdk": "8.1.0", "common": "8.0.0"}

    def test_parse_workspace_conflict_no_match(self):
        stderr = "Some other error message that's not a workspace conflict"
        conflicts = parse_workspace_conflict(stderr)
        assert conflicts is None

    def test_parse_workspace_conflict_different_extras(self):
        stderr = """
No solution found when resolving dependencies:
  Because common[dev] depends on flake8==7.2.0 and qluster-sdk[test] depends on flake8==7.3.0, we can resolve the conflict.
"""
        conflicts = parse_workspace_conflict(stderr)
        # Should not match conflicts with different extra names
        assert conflicts == []

    def test_parse_workspace_conflict_different_packages(self):
        stderr = """
No solution found when resolving dependencies:
  Because common[dev] depends on flake8==7.2.0 and qluster-sdk[dev] depends on black==23.0.0, we can resolve the conflict.
"""
        conflicts = parse_workspace_conflict(stderr)
        # Should not match conflicts with different package names
        assert conflicts == []


class TestTargetVersionDetermination:
    def test_determine_target_versions_latest_policy(self):
        conflicts = [
            WorkspaceConflict("flake8", "dev", {"common": "7.2.0", "qluster-sdk": "7.3.0"}),
            WorkspaceConflict("pytest", "dev", {"common": "8.0.0", "qluster-sdk": "8.1.0"})
        ]
        
        with patch('uvrepin.main.get_latest_version') as mock_latest:
            mock_latest.side_effect = lambda pkg, idx, pre: {"flake8": "7.4.0", "pytest": "8.2.0"}[pkg]
            
            target_versions = determine_target_versions(conflicts, "latest")
            
            assert target_versions == {"flake8": "7.4.0", "pytest": "8.2.0"}

    def test_determine_target_versions_max_policy(self):
        conflicts = [
            WorkspaceConflict("flake8", "dev", {"common": "7.2.0", "qluster-sdk": "7.3.0"}),
            WorkspaceConflict("pytest", "dev", {"common": "8.1.0", "qluster-sdk": "8.0.0"})
        ]
        
        target_versions = determine_target_versions(conflicts, "max")
        
        assert target_versions == {"flake8": "7.3.0", "pytest": "8.1.0"}

    def test_determine_target_versions_fallback_to_max(self):
        conflicts = [
            WorkspaceConflict("flake8", "dev", {"common": "7.2.0", "qluster-sdk": "7.3.0"})
        ]
        
        with patch('uvrepin.main.get_latest_version') as mock_latest:
            mock_latest.return_value = "unknown"
            
            target_versions = determine_target_versions(conflicts, "latest")
            
            assert target_versions == {"flake8": "7.3.0"}


class TestCIEnvironment:
    def test_is_ci_environment_true_values(self):
        for value in ["true", "TRUE", "1", "yes", "YES"]:
            with patch.dict(os.environ, {"CI": value}):
                assert is_ci_environment() == True

    def test_is_ci_environment_false_values(self):
        for value in ["false", "0", "no", ""]:
            with patch.dict(os.environ, {"CI": value}):
                assert is_ci_environment() == False

    def test_is_ci_environment_no_env(self):
        with patch.dict(os.environ, {}, clear=True):
            assert is_ci_environment() == False


class TestWorkspaceAlignment:
    def test_align_workspace_members_success(self):
        conflicts = [
            WorkspaceConflict("flake8", "dev", {"common": "7.2.0", "qluster-sdk": "7.3.0"})
        ]
        resolution = ConflictResolution(
            extra_name="dev",
            conflicts=conflicts,
            target_versions={"flake8": "7.4.0"},
            affected_members={"common", "qluster-sdk"}
        )
        
        with patch.object(uv_runner, 'run') as mock_run:
            # Mock successful uv add, uv lock, and uv sync calls
            mock_run.return_value = MagicMock(returncode=0)
            
            result = align_workspace_members(resolution, sync=True, indexes=[], allow_pre=False)
            
            assert result == True
            
            # Check that the expected calls were made (may not be in exact order for member calls)
            actual_calls = [call[0] for call in mock_run.call_args_list]
            
            # Check uv add calls for both members
            member_calls = [call for call in actual_calls if "add" in call and "--project" in call]
            assert len(member_calls) == 2
            assert any("common" in call and "flake8==7.4.0" in call for call in member_calls)
            assert any("qluster-sdk" in call and "flake8==7.4.0" in call for call in member_calls)
            
            # Check uv lock was called  
            lock_calls = [call for call in actual_calls if len(call) >= 2 and call[:2] == ("uv", "lock")]
            assert len(lock_calls) == 1
            
            # Check uv sync was called
            sync_calls = [call for call in actual_calls if len(call) >= 2 and call[:2] == ("uv", "sync")]
            assert len(sync_calls) == 1

    def test_align_workspace_members_no_sync(self):
        conflicts = [
            WorkspaceConflict("flake8", "dev", {"common": "7.2.0", "qluster-sdk": "7.3.0"})
        ]
        resolution = ConflictResolution(
            extra_name="dev",
            conflicts=conflicts,
            target_versions={"flake8": "7.4.0"},
            affected_members={"common", "qluster-sdk"}
        )
        
        with patch.object(uv_runner, 'run') as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            
            result = align_workspace_members(resolution, sync=False, indexes=[], allow_pre=False)
            
            assert result == True
            
            # Verify sync was not called
            actual_calls = [call[0] for call in mock_run.call_args_list]
            sync_calls = [call for call in actual_calls if "sync" in call]
            assert len(sync_calls) == 0

    def test_align_workspace_members_add_failure(self):
        conflicts = [
            WorkspaceConflict("flake8", "dev", {"common": "7.2.0", "qluster-sdk": "7.3.0"})
        ]
        resolution = ConflictResolution(
            extra_name="dev",
            conflicts=conflicts,
            target_versions={"flake8": "7.4.0"},
            affected_members={"common", "qluster-sdk"}
        )
        
        with patch.object(uv_runner, 'run') as mock_run:
            # First call (common) succeeds, second call (qluster-sdk) fails
            mock_run.side_effect = [
                MagicMock(returncode=0),  # common succeeds
                MagicMock(returncode=1)   # qluster-sdk fails
            ]
            
            result = align_workspace_members(resolution, sync=False, indexes=[], allow_pre=False)
            
            assert result == False

    def test_align_workspace_members_lock_failure(self):
        conflicts = [
            WorkspaceConflict("flake8", "dev", {"common": "7.2.0", "qluster-sdk": "7.3.0"})
        ]
        resolution = ConflictResolution(
            extra_name="dev",
            conflicts=conflicts,
            target_versions={"flake8": "7.4.0"},
            affected_members={"common", "qluster-sdk"}
        )
        
        with patch.object(uv_runner, 'run') as mock_run:
            # uv add calls succeed, uv lock fails
            mock_run.side_effect = [
                MagicMock(returncode=0),  # common uv add
                MagicMock(returncode=0),  # qluster-sdk uv add
                MagicMock(returncode=1)   # uv lock fails
            ]
            
            result = align_workspace_members(resolution, sync=False, indexes=[], allow_pre=False)
            
            assert result == False

    def test_align_workspace_members_multiple_packages(self):
        conflicts = [
            WorkspaceConflict("flake8", "dev", {"common": "7.2.0", "qluster-sdk": "7.3.0"}),
            WorkspaceConflict("pytest", "dev", {"common": "8.0.0", "qluster-sdk": "8.1.0"})
        ]
        resolution = ConflictResolution(
            extra_name="dev",
            conflicts=conflicts,
            target_versions={"flake8": "7.4.0", "pytest": "8.2.0"},
            affected_members={"common", "qluster-sdk"}
        )
        
        with patch.object(uv_runner, 'run') as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            
            result = align_workspace_members(resolution, sync=False, indexes=[], allow_pre=False)
            
            assert result == True
            
            # Verify both packages are included in each member's uv add call
            actual_calls = [call[0] for call in mock_run.call_args_list]
            common_call = actual_calls[0]
            qluster_call = actual_calls[1]
            
            assert "flake8==7.4.0" in common_call
            assert "pytest==8.2.0" in common_call
            assert "flake8==7.4.0" in qluster_call
            assert "pytest==8.2.0" in qluster_call


class TestInteractivePrompts:
    def test_prompt_user_for_conflict_resolution_yes(self):
        conflicts = [
            WorkspaceConflict("flake8", "dev", {"common": "7.2.0", "qluster-sdk": "7.3.0"})
        ]
        target_versions = {"flake8": "7.4.0"}
        
        with patch('builtins.input', return_value='y'):
            result = prompt_user_for_conflict_resolution(conflicts, target_versions)
            assert result == True

    def test_prompt_user_for_conflict_resolution_no(self):
        conflicts = [
            WorkspaceConflict("flake8", "dev", {"common": "7.2.0", "qluster-sdk": "7.3.0"})
        ]
        target_versions = {"flake8": "7.4.0"}
        
        with patch('builtins.input', return_value='n'):
            result = prompt_user_for_conflict_resolution(conflicts, target_versions)
            assert result == False

    def test_prompt_user_for_conflict_resolution_empty(self):
        conflicts = [
            WorkspaceConflict("flake8", "dev", {"common": "7.2.0", "qluster-sdk": "7.3.0"})
        ]
        target_versions = {"flake8": "7.4.0"}
        
        with patch('builtins.input', return_value=''):
            result = prompt_user_for_conflict_resolution(conflicts, target_versions)
            assert result == False

    def test_show_manual_resolution_help(self, capsys):
        conflicts = [
            WorkspaceConflict("flake8", "dev", {"common": "7.2.0", "qluster-sdk": "7.3.0"})
        ]
        
        show_manual_resolution_help(conflicts)
        
        captured = capsys.readouterr()
        assert "manually resolve these conflicts" in captured.out
        assert "uv add --project common --optional dev flake8==<target_version>" in captured.out
        assert "uv add --project qluster-sdk --optional dev flake8==<target_version>" in captured.out
        assert "uv lock" in captured.out


class TestWorkspaceConflictIntegration:
    def create_workspace(self, tmp_path):
        """Create a test workspace with conflicting dependencies."""
        # Root pyproject.toml - needs some dependencies to update
        root_pyproject = """[tool.uv.workspace]
members = ["common", "qluster_sdk"]

[project]
name = "workspace-root"
version = "0.1.0"
dependencies = []

[project.optional-dependencies]
dev = [
    "requests==2.28.0"
]
"""
        (tmp_path / "pyproject.toml").write_text(root_pyproject)
        
        # Common member
        common_dir = tmp_path / "common"
        common_dir.mkdir()
        common_pyproject = """[project]
name = "common"
version = "0.1.0"
dependencies = []

[project.optional-dependencies]
dev = [
    "flake8==7.2.0",
    "pytest==8.0.0"
]
"""
        (common_dir / "pyproject.toml").write_text(common_pyproject)
        
        # Qluster SDK member
        sdk_dir = tmp_path / "qluster_sdk"
        sdk_dir.mkdir()
        sdk_pyproject = """[project]
name = "qluster-sdk"
version = "0.1.0"
dependencies = []

[project.optional-dependencies]
dev = [
    "flake8==7.3.0",
    "pytest==8.1.0"
]
"""
        (sdk_dir / "pyproject.toml").write_text(sdk_pyproject)
        
        return tmp_path

    def test_workspace_conflict_interactive_accept(self, tmp_path, capsys):
        """Test happy path with interactive acceptance."""
        workspace_root = self.create_workspace(tmp_path)
        
        # Mock uvrepin's core dependencies
        mock_outdated_output = """Package    Version    Latest     Type
--------   -------    ------     ----
requests   2.28.0     2.31.0     wheel
flake8     7.2.0      7.4.0      wheel
pytest     8.0.0      8.2.0      wheel"""

        # Mock conflict stderr
        conflict_stderr = """
No solution found when resolving dependencies:
  Because common[dev] depends on flake8==7.2.0 and qluster-sdk[dev] depends on flake8==7.3.0, we can resolve the conflict.
  Because common[dev] depends on pytest==8.0.0 and qluster-sdk[dev] depends on pytest==8.1.0, we can resolve the conflict.
"""

        with patch.object(uv_runner, 'run') as mock_run, \
             patch('uvrepin.main.PYPROJECT', workspace_root / "pyproject.toml"), \
             patch('builtins.input', return_value='y'), \
             patch('os.chdir'):
            
            # Mock sequence: uv version check, outdated check, uv add (fails), then resolution sequence
            mock_run.side_effect = [
                MagicMock(returncode=0),  # uv --version
                MagicMock(returncode=0, stdout=mock_outdated_output),  # uv pip list --outdated
                MagicMock(returncode=1, stderr=conflict_stderr, stdout=""),  # uv add (fails with conflict)
                MagicMock(returncode=0, stdout=mock_outdated_output),  # get_latest_version for flake8
                MagicMock(returncode=0, stdout=mock_outdated_output),  # get_latest_version for pytest
                # Resolution sequence
                MagicMock(returncode=0),  # uv add common
                MagicMock(returncode=0),  # uv add qluster-sdk
                MagicMock(returncode=0),  # uv lock
            ]
            
            os.chdir(workspace_root)
            with patch('sys.argv', ['uvrepin']):
                exit_code = main()
            
            assert exit_code == 0
            captured = capsys.readouterr()
            assert "Conflicts detected in extra \"dev\"" in captured.out
            assert "flake8: common(==7.2.0) ↔ qluster-sdk(==7.3.0)" in captured.out
            assert "pytest: common(==8.0.0) ↔ qluster-sdk(==8.1.0)" in captured.out
            assert "Workspace conflicts resolved successfully" in captured.out

    def test_workspace_conflict_auto_accept_yes_flag(self, tmp_path, capsys):
        """Test auto-acceptance with --yes flag."""
        workspace_root = self.create_workspace(tmp_path)
        
        mock_outdated_output = """Package    Version    Latest     Type
--------   -------    ------     ----
requests   2.28.0     2.31.0     wheel
flake8     7.2.0      7.4.0      wheel"""

        conflict_stderr = """
No solution found when resolving dependencies:
  Because common[dev] depends on flake8==7.2.0 and qluster-sdk[dev] depends on flake8==7.3.0, we can resolve the conflict.
"""

        with patch.object(uv_runner, 'run') as mock_run, \
             patch('uvrepin.main.PYPROJECT', workspace_root / "pyproject.toml"), \
             patch('os.chdir'):
            
            mock_run.side_effect = [
                MagicMock(returncode=0),  # uv --version
                MagicMock(returncode=0, stdout=mock_outdated_output),  # uv pip list --outdated
                MagicMock(returncode=1, stderr=conflict_stderr, stdout=""),  # uv add (fails with conflict)
                MagicMock(returncode=0, stdout=mock_outdated_output),  # get_latest_version for flake8
                MagicMock(returncode=0),  # uv add common
                MagicMock(returncode=0),  # uv add qluster-sdk
                MagicMock(returncode=0),  # uv lock
            ]
            
            os.chdir(workspace_root)
            with patch('sys.argv', ['uvrepin', '--yes']):
                exit_code = main()
            
            assert exit_code == 0
            captured = capsys.readouterr()
            assert "Auto-accepting workspace conflict resolution" in captured.out
            # Should not prompt user
            assert "Align all pyproject.toml files" not in captured.out

    def test_workspace_conflict_auto_accept_ci(self, tmp_path, capsys):
        """Test auto-acceptance in CI environment."""
        workspace_root = self.create_workspace(tmp_path)
        
        mock_outdated_output = """Package    Version    Latest     Type
--------   -------    ------     ----
requests   2.28.0     2.31.0     wheel
flake8     7.2.0      7.4.0      wheel"""

        conflict_stderr = """
No solution found when resolving dependencies:
  Because common[dev] depends on flake8==7.2.0 and qluster-sdk[dev] depends on flake8==7.3.0, we can resolve the conflict.
"""

        with patch.object(uv_runner, 'run') as mock_run, \
             patch('uvrepin.main.PYPROJECT', workspace_root / "pyproject.toml"), \
             patch.dict(os.environ, {"CI": "true"}), \
             patch('os.chdir'):
            
            mock_run.side_effect = [
                MagicMock(returncode=0),  # uv --version
                MagicMock(returncode=0, stdout=mock_outdated_output),  # uv pip list --outdated
                MagicMock(returncode=1, stderr=conflict_stderr, stdout=""),  # uv add (fails with conflict)
                MagicMock(returncode=0, stdout=mock_outdated_output),  # get_latest_version for flake8
                MagicMock(returncode=0),  # uv add common
                MagicMock(returncode=0),  # uv add qluster-sdk
                MagicMock(returncode=0),  # uv lock
            ]
            
            os.chdir(workspace_root)
            with patch('sys.argv', ['uvrepin']):
                exit_code = main()
            
            assert exit_code == 0
            captured = capsys.readouterr()
            assert "Auto-accepting workspace conflict resolution" in captured.out

    def test_workspace_conflict_user_declines(self, tmp_path, capsys):
        """Test user declining resolution."""
        workspace_root = self.create_workspace(tmp_path)
        
        mock_outdated_output = """Package    Version    Latest     Type
--------   -------    ------     ----
requests   2.28.0     2.31.0     wheel
flake8     7.2.0      7.4.0      wheel"""

        conflict_stderr = """
No solution found when resolving dependencies:
  Because common[dev] depends on flake8==7.2.0 and qluster-sdk[dev] depends on flake8==7.3.0, we can resolve the conflict.
"""

        with patch.object(uv_runner, 'run') as mock_run, \
             patch('uvrepin.main.PYPROJECT', workspace_root / "pyproject.toml"), \
             patch('builtins.input', return_value='n'), \
             patch('os.chdir'):
            
            mock_run.side_effect = [
                MagicMock(returncode=0),  # uv --version
                MagicMock(returncode=0, stdout=mock_outdated_output),  # uv pip list --outdated
                MagicMock(returncode=1, stderr=conflict_stderr, stdout=""),  # uv add (fails with conflict)
                MagicMock(returncode=0, stdout=mock_outdated_output),  # get_latest_version for flake8
            ]
            
            os.chdir(workspace_root)
            with patch('sys.argv', ['uvrepin']):
                exit_code = main()
            
            assert exit_code == 0
            captured = capsys.readouterr()
            assert "manually resolve these conflicts" in captured.out
            assert "uv add --project common --optional dev flake8==<target_version>" in captured.out

    def test_workspace_conflict_with_sync(self, tmp_path):
        """Test workspace conflict resolution with --sync flag."""
        workspace_root = self.create_workspace(tmp_path)
        
        mock_outdated_output = """Package    Version    Latest     Type
--------   -------    ------     ----
requests   2.28.0     2.31.0     wheel
flake8     7.2.0      7.4.0      wheel"""

        conflict_stderr = """
No solution found when resolving dependencies:
  Because common[dev] depends on flake8==7.2.0 and qluster-sdk[dev] depends on flake8==7.3.0, we can resolve the conflict.
"""

        with patch.object(uv_runner, 'run') as mock_run, \
             patch('uvrepin.main.PYPROJECT', workspace_root / "pyproject.toml"), \
             patch('os.chdir'):
            
            mock_run.side_effect = [
                MagicMock(returncode=0),  # uv --version
                MagicMock(returncode=0, stdout=mock_outdated_output),  # uv pip list --outdated
                MagicMock(returncode=1, stderr=conflict_stderr, stdout=""),  # uv add (fails with conflict)
                MagicMock(returncode=0, stdout=mock_outdated_output),  # get_latest_version for flake8
                MagicMock(returncode=0),  # uv add common
                MagicMock(returncode=0),  # uv add qluster-sdk
                MagicMock(returncode=0),  # uv lock
                MagicMock(returncode=0),  # uv sync
            ]
            
            os.chdir(workspace_root)
            with patch('sys.argv', ['uvrepin', '--yes', '--sync']):
                exit_code = main()
            
            assert exit_code == 0
            
            # Verify sync was called
            actual_calls = [call[0] for call in mock_run.call_args_list]
            sync_calls = [call for call in actual_calls if len(call) >= 2 and call[:2] == ("uv", "sync")]
            assert len(sync_calls) == 1

    def test_not_workspace_conflict_error(self, tmp_path, capsys):
        """Test handling non-workspace conflict errors."""
        workspace_root = self.create_workspace(tmp_path)
        
        mock_outdated_output = """Package    Version    Latest     Type
--------   -------    ------     ----
requests   2.28.0     2.31.0     wheel
flake8     7.2.0      7.4.0      wheel"""

        other_error = "Some other uv error that's not a workspace conflict"

        with patch.object(uv_runner, 'run') as mock_run, \
             patch('uvrepin.main.PYPROJECT', workspace_root / "pyproject.toml"), \
             patch('os.chdir'):
            
            mock_run.side_effect = [
                MagicMock(returncode=0),  # uv --version
                MagicMock(returncode=0, stdout=mock_outdated_output),  # uv pip list --outdated
                MagicMock(returncode=1, stderr=other_error, stdout=""),  # uv add (fails with other error)
            ]
            
            os.chdir(workspace_root)
            with pytest.raises(SystemExit) as exc_info:
                with patch('sys.argv', ['uvrepin']):
                    main()
            
            assert exc_info.value.code != 0
            # Should not trigger workspace conflict resolution
            captured = capsys.readouterr()
            assert "Conflicts detected" not in captured.out

    def test_lock_fails_after_alignment(self, tmp_path):
        """Test when uv lock fails after successful alignment."""
        workspace_root = self.create_workspace(tmp_path)
        
        mock_outdated_output = """Package    Version    Latest     Type
--------   -------    ------     ----
requests   2.28.0     2.31.0     wheel
flake8     7.2.0      7.4.0      wheel"""

        conflict_stderr = """
No solution found when resolving dependencies:
  Because common[dev] depends on flake8==7.2.0 and qluster-sdk[dev] depends on flake8==7.3.0, we can resolve the conflict.
"""

        with patch.object(uv_runner, 'run') as mock_run, \
             patch('uvrepin.main.PYPROJECT', workspace_root / "pyproject.toml"), \
             patch('os.chdir'):
            
            mock_run.side_effect = [
                MagicMock(returncode=0),  # uv --version
                MagicMock(returncode=0, stdout=mock_outdated_output),  # uv pip list --outdated
                MagicMock(returncode=1, stderr=conflict_stderr, stdout=""),  # uv add (fails with conflict)
                MagicMock(returncode=0, stdout=mock_outdated_output),  # get_latest_version for flake8
                MagicMock(returncode=0),  # uv add common
                MagicMock(returncode=0),  # uv add qluster-sdk
                MagicMock(returncode=1),  # uv lock (fails)
            ]
            
            os.chdir(workspace_root)
            with pytest.raises(SystemExit) as exc_info:
                with patch('sys.argv', ['uvrepin', '--yes']):
                    main()
            
            assert exc_info.value.code == 1