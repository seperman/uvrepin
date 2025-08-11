import os
import tempfile
import subprocess
from pathlib import Path
from unittest.mock import patch, MagicMock
import pytest
from uvrepin.main import main, parse_req, gather_direct, parse_outdated_table


class TestParseReq:
    def test_parse_simple_requirement(self):
        result = parse_req("requests==2.28.1")
        assert result == ("requests", "", "2.28.1", None)

    def test_parse_requirement_with_extras(self):
        result = parse_req("fastapi[uvicorn]==0.95.2")
        assert result == ("fastapi", "[uvicorn]", "0.95.2", None)

    def test_parse_requirement_with_marker(self):
        result = parse_req("pytest==7.4.0; python_version >= '3.8'")
        assert result == ("pytest", "", "7.4.0", "python_version >= '3.8'")

    def test_parse_requirement_no_version(self):
        result = parse_req("requests")
        assert result == ("requests", "", None, None)

    def test_parse_skip_vcs_requirement(self):
        result = parse_req("git+https://github.com/user/repo.git")
        assert result == ("SKIP", "", None, None)

    def test_parse_empty_requirement(self):
        result = parse_req("  ")
        assert result is None

    def test_parse_comment_requirement(self):
        result = parse_req("# this is a comment")
        assert result is None


class TestGatherDirect:
    def test_gather_main_dependencies(self):
        data = {
            "project": {
                "dependencies": [
                    "requests==2.28.1",
                    "fastapi[uvicorn]==0.95.2"
                ]
            }
        }
        result = gather_direct(data)
        assert None in result
        assert len(result[None]) == 2
        assert result[None][0]["name"] == "requests"
        assert result[None][1]["name"] == "fastapi"

    def test_gather_dependency_groups(self):
        data = {
            "dependency-groups": {
                "dev": ["pytest==7.4.0", "black==23.7.0"],
                "test": ["coverage==7.2.0"]
            }
        }
        result = gather_direct(data)
        assert "dev" in result
        assert "test" in result
        assert len(result["dev"]) == 2
        assert len(result["test"]) == 1


class TestParseOutdatedTable:
    def test_parse_simple_table(self):
        table_text = """Package    Version    Latest     Type
--------   -------    ------     ----
requests   2.28.0     2.28.1     wheel
fastapi    0.95.1     0.95.2     wheel"""
        result = parse_outdated_table(table_text)
        assert result["requests"] == "2.28.1"
        assert result["fastapi"] == "0.95.2"

    def test_parse_table_with_separators(self):
        table_text = """Package    Version    Latest     Type
--------   -------    ------     ----
requests   2.28.0     2.28.1     wheel
---------  --------   --------   -----
fastapi    0.95.1     0.95.2     wheel"""
        result = parse_outdated_table(table_text)
        assert result["requests"] == "2.28.1"
        assert result["fastapi"] == "0.95.2"

    def test_parse_empty_table(self):
        table_text = """Package    Version    Latest     Type
--------   -------    ------     ----

"""
        result = parse_outdated_table(table_text)
        # The result may contain entries from separator lines, but no actual packages
        assert len([k for k in result.keys() if not k.startswith("-")]) == 0


class TestUvrepinCLI:
    def create_sample_pyproject(self, tmp_path):
        """Create a sample pyproject.toml for testing"""
        pyproject_content = """[project]
name = "test-project"
dependencies = [
    "requests==2.28.0",
    "fastapi==0.95.1"
]

[dependency-groups]
dev = [
    "pytest==7.3.0",
    "black==23.6.0"
]
"""
        pyproject_path = tmp_path / "pyproject.toml"
        pyproject_path.write_text(pyproject_content)
        return pyproject_path

    def test_dry_run_with_outdated_deps(self, tmp_path, capsys):
        """Test dry run shows what would be updated"""
        self.create_sample_pyproject(tmp_path)
        
        # Mock the uv commands
        mock_outdated_output = """Package    Version    Latest     Type
--------   -------    ------     ----
requests   2.28.0     2.28.1     wheel
fastapi    0.95.1     0.95.2     wheel
pytest     7.3.0      7.4.0      wheel"""

        with patch('uvrepin.main.subprocess.run') as mock_run, \
             patch('uvrepin.main.PYPROJECT', tmp_path / "pyproject.toml"):
            
            # Mock uv --version check
            mock_run.side_effect = [
                MagicMock(returncode=0),  # uv --version
                MagicMock(returncode=0, stdout=mock_outdated_output)  # uv pip list --outdated
            ]
            
            # Test dry run
            with patch('sys.argv', ['uvrepin', '--dry-run']):
                exit_code = main()
                
            assert exit_code == 0
            captured = capsys.readouterr()
            assert "Dry run — would update these direct dependencies:" in captured.out
            assert "requests" in captured.out
            assert "fastapi" in captured.out
            assert "2.28.0" in captured.out
            assert "2.28.1" in captured.out

    def test_dry_run_no_outdated_deps(self, tmp_path, capsys):
        """Test dry run when no dependencies are outdated"""
        self.create_sample_pyproject(tmp_path)
        
        with patch('uvrepin.main.subprocess.run') as mock_run, \
             patch('uvrepin.main.PYPROJECT', tmp_path / "pyproject.toml"):
            
            # Mock uv commands with no outdated packages
            mock_run.side_effect = [
                MagicMock(returncode=0),  # uv --version
                MagicMock(returncode=0, stdout="Package    Version    Latest     Type\n--------   -------    ------     ----")  # empty outdated
            ]
            
            with patch('sys.argv', ['uvrepin', '--dry-run']):
                exit_code = main()
                
            assert exit_code == 0
            captured = capsys.readouterr()
            assert "nothing to update" in captured.out

    def test_update_dependencies(self, tmp_path):
        """Test actual dependency update (mocked)"""
        self.create_sample_pyproject(tmp_path)
        
        mock_outdated_output = """Package    Version    Latest     Type
--------   -------    ------     ----
requests   2.28.0     2.28.1     wheel"""

        with patch('uvrepin.main.subprocess.run') as mock_run, \
             patch('uvrepin.main.PYPROJECT', tmp_path / "pyproject.toml"):
            
            mock_run.side_effect = [
                MagicMock(returncode=0),  # uv --version
                MagicMock(returncode=0, stdout=mock_outdated_output),  # uv pip list --outdated
                MagicMock(returncode=0)  # uv add requests==2.28.1
            ]
            
            with patch('sys.argv', ['uvrepin']):
                exit_code = main()
                
            assert exit_code is None or exit_code == 0
            # Verify uv add was called with the right arguments
            mock_run.assert_any_call(['uv', 'add', '--no-sync', 'requests==2.28.1'])

    def test_only_groups_filter(self, tmp_path, capsys):
        """Test --only-groups filter"""
        self.create_sample_pyproject(tmp_path)
        
        mock_outdated_output = """Package    Version    Latest     Type
--------   -------    ------     ----
pytest     7.3.0      7.4.0      wheel"""

        with patch('uvrepin.main.subprocess.run') as mock_run, \
             patch('uvrepin.main.PYPROJECT', tmp_path / "pyproject.toml"):
            
            mock_run.side_effect = [
                MagicMock(returncode=0),  # uv --version
                MagicMock(returncode=0, stdout=mock_outdated_output)  # uv pip list --outdated
            ]
            
            with patch('sys.argv', ['uvrepin', '--dry-run', '--only-groups', 'dev']):
                exit_code = main()
                
            assert exit_code == 0
            captured = capsys.readouterr()
            assert "pytest" in captured.out

    def test_no_pyproject_toml(self, tmp_path):
        """Test behavior when pyproject.toml doesn't exist"""
        with patch('uvrepin.main.PYPROJECT', tmp_path / "nonexistent.toml"), \
             pytest.raises(SystemExit) as exc_info:
            with patch('sys.argv', ['uvrepin']):
                main()
        
        assert exc_info.value.code == 1

    def test_uv_not_found(self):
        """Test behavior when uv is not available"""
        with patch('uvrepin.main.subprocess.run') as mock_run:
            mock_run.side_effect = subprocess.CalledProcessError(1, 'uv')
            
            with pytest.raises(SystemExit) as exc_info:
                with patch('sys.argv', ['uvrepin']):
                    main()
            
            assert exc_info.value.code == 1


class TestCLIIntegration:
    """Integration tests that work with real files but mock network calls"""
    
    def test_cli_entry_point(self, tmp_path):
        """Test that the CLI entry point works"""
        # Create a temporary pyproject.toml
        pyproject_content = """[project]
name = "test-project"
dependencies = [
    "requests==2.28.0"
]
"""
        pyproject_path = tmp_path / "pyproject.toml"
        pyproject_path.write_text(pyproject_content)
        
        # Change to the temp directory
        original_cwd = os.getcwd()
        os.chdir(tmp_path)
        
        try:
            # Mock the subprocess calls but use real file system
            with patch('subprocess.run') as mock_run:
                mock_run.side_effect = [
                    MagicMock(returncode=0),  # uv --version
                    MagicMock(returncode=0, stdout="Package    Version    Latest     Type\n--------   -------    ------     ----")
                ]
                
                # Import and run the main function
                from uvrepin import main
                with patch('sys.argv', ['uvrepin', '--dry-run']):
                    result = main()
                
                assert result == 0
        finally:
            os.chdir(original_cwd)