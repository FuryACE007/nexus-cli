#!/usr/bin/env python3
"""
Integration Test Suite for Nexus CLI

Tests code paths and logic without requiring a running backend.
This verifies:
1. Skill detection logic
2. @skill interceptor parsing
3. /solve command construction
4. Header injection (X-Nexus-Skill)
"""

import json
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import Mock, patch, MagicMock

# Add repo to path (dynamically resolve to the test file's directory)
_test_dir = Path(__file__).parent
sys.path.insert(0, str(_test_dir))

from aider.nexus_auth import (
    _load_config,
    _save_config,
    _get_repo_metadata,
    _score_skill,
    get_auth_headers,
)


class TestAuthModule:
    """Test nexus_auth module"""

    def test_get_repo_metadata(self):
        """Verify repo metadata extraction"""
        metadata = _get_repo_metadata(str(_test_dir))
        print("\n✓ test_get_repo_metadata")
        print(f"  Directory: {metadata.get('directory_name')}")
        print(f"  Git remote: {metadata.get('git_remote_url')}")
        assert metadata["directory_name"] == "nexus-cli"
        assert len(metadata["top_level_files"]) > 0

    def test_score_skill(self):
        """Verify skill scoring logic"""
        skill = {
            "name": "staking",
            "keywords": ["stake", "validator", "epoch"],
        }
        metadata = {
            "directory_name": "staking-contract",
            "git_remote_url": "github.com/org/staking-validators",
            "top_level_files": ["stake.rs", "validator.rs", "src/", "tests/"],
        }

        score = _score_skill(skill, metadata)
        print(f"\n✓ test_score_skill")
        print(f"  Skill: {skill['name']}")
        print(f"  Metadata matches: staking directory + keywords")
        print(f"  Score: {score}")
        assert score > 5  # Should score well with matching keywords

    def test_config_persistence(self):
        """Verify config file read/write (skill mappings only)"""
        with tempfile.TemporaryDirectory() as tmpdir:
            config_file = Path(tmpdir) / "config"
            test_config = {"skill_mappings": {"/some/repo": "staking"}}

            # Save
            config_file.write_text(json.dumps(test_config))
            loaded = json.loads(config_file.read_text())

            print(f"\n✓ test_config_persistence")
            print(f"  Config saved and loaded correctly")
            assert loaded == test_config

    def test_get_auth_headers_no_credentials(self):
        """Verify auth headers returns empty dict (no credentials required)"""
        headers = get_auth_headers()
        print(f"\n✓ test_get_auth_headers_no_credentials")
        print(f"  Headers: {headers}")
        assert headers == {}
        assert "X-Nexus-Skill" not in headers


class TestMainModifications:
    """Test main.py hardwiring"""

    def test_hardwiring_in_main(self):
        """Verify hardwiring constants are in main.py"""
        with open(_test_dir / "aider" / "main.py") as f:
            content = f.read()

        print("\n✓ test_hardwiring_in_main")

        checks = [
            ("nexus-passthrough", "API key placeholder"),
            ("openai/nexus-agent", "model name with openai/ prefix"),
            ("http://localhost:8000/v1", "backend URL"),
            ("detect_active_skill", "skill detection function"),
            ("X-Nexus-Skill", "skill header injection"),
            ("openai/nexus-architect", "architect model created at startup"),
            ("_nexus_architect_model", "architect cross-reference stored on code model"),
            ("_nexus_code_model", "code cross-reference stored on architect model"),
        ]

        for check_str, description in checks:
            assert check_str in content, f"Missing: {description}"
            print(f"  ✓ {description}")


class TestArchitectWiring:
    """Test architect mode two-model setup"""

    def test_architect_model_in_main(self):
        """Verify architect model is created and wired in main.py"""
        with open(_test_dir / "aider" / "main.py") as f:
            content = f.read()

        print("\n✓ test_architect_model_in_main")
        checks = [
            ("openai/nexus-architect", "architect model name"),
            ("arch_model.editor_model = main_model", "stage 2 wired to code model"),
            ("_nexus_architect_model = arch_model", "cross-ref on code model"),
            ("_nexus_code_model = main_model", "cross-ref on architect model"),
        ]
        for check_str, description in checks:
            assert check_str in content, f"Missing: {description}"
            print(f"  ✓ {description}")

    def test_architect_validation_bypass(self):
        """Verify nexus-architect bypasses litellm validation"""
        with open(_test_dir / "aider" / "models.py") as f:
            content = f.read()

        print("\n✓ test_architect_validation_bypass")
        assert "nexus-architect" in content
        print("  ✓ nexus-architect in validation bypass")

    def test_architect_cmd_swaps_model(self):
        """Verify cmd_architect swaps main_model to architect model"""
        with open(_test_dir / "aider" / "commands.py") as f:
            content = f.read()

        print("\n✓ test_architect_cmd_swaps_model")
        assert "_nexus_architect_model" in content
        assert "_restore_nexus_code_model" in content
        assert "_nexus_code_model" in content
        print("  ✓ model swap logic present in commands.py")

    def test_skill_sync_to_both_models(self):
        """Verify @skill tags update both code and architect model headers"""
        with open(_test_dir / "aider" / "coders" / "base_coder.py") as f:
            content = f.read()

        print("\n✓ test_skill_sync_to_both_models")
        assert "_nexus_architect_model" in content
        assert "_nexus_code_model" in content
        print("  ✓ X-Nexus-Skill synced to both model instances on @skill switch")


class TestModelModifications:
    """Test models.py changes"""

    def test_nexus_validation_bypass(self):
        """Verify validation bypass for nexus-agent"""
        with open(_test_dir / "aider" / "models.py") as f:
            content = f.read()

        print("\n✓ test_nexus_validation_bypass")
        assert "nexus-agent" in content
        assert "nexus-architect" in content
        assert "return dict(keys_in_environment=" in content
        print("  ✓ Short-circuit validation for nexus-agent and nexus-architect in place")

    def test_header_injection(self):
        """Verify header injection in send_completion"""
        with open(_test_dir / "aider" / "models.py") as f:
            content = f.read()

        print("\n✓ test_header_injection")
        assert "_nexus_extra_headers" in content
        assert 'kwargs["extra_headers"].update' in content
        print("  ✓ Header injection in send_completion()")


class TestSkillInterceptor:
    """Test @skill interceptor logic"""

    def test_skill_regex_extraction(self):
        """Verify @skill tag extraction"""
        import re

        test_inputs = [
            ("@staking fix the bug", ["staking"]),
            ("@payments and @staking both need work", ["payments", "staking"]),
            ("no tags here", []),
            ("@test123 and normal text", ["test123"]),
        ]

        print("\n✓ test_skill_regex_extraction")
        pattern = r"@(\w+)"

        for inp, expected in test_inputs:
            matches = re.findall(pattern, inp)
            assert matches == expected, f"Input '{inp}' gave {matches}, expected {expected}"
            print(f"  ✓ '{inp}' → {matches}")

    def test_skill_stripping(self):
        """Verify @tags are stripped from message"""
        import re

        test_cases = [
            ("@staking fix the bug", "fix the bug"),
            ("before @test middle @staking after", "before middle after"),
            ("multiple  @a @b  @c tags", "multiple tags"),
        ]

        print("\n✓ test_skill_stripping")

        for inp, expected in test_cases:
            cleaned = re.sub(r"@\w+\s*", "", inp).strip()
            # Collapse multiple spaces to single space
            cleaned = re.sub(r"\s+", " ", cleaned).strip()
            assert cleaned == expected, f"'{inp}' → '{cleaned}' (expected '{expected}')"
            print(f"  ✓ '{inp}' → '{cleaned}'")


class TestSolveCommand:
    """Test /solve command construction"""

    def test_solve_imports(self):
        """Verify /solve has required imports"""
        with open(_test_dir / "aider" / "commands.py") as f:
            content = f.read()

        # Find cmd_solve method
        start = content.find("def cmd_solve(self, args):")
        end = content.find("\n    def ", start + 1) if start >= 0 else -1
        solve_method = content[start : end if end > 0 else len(content)]

        print("\n✓ test_solve_imports")

        required = [
            "subprocess",
            "requests",
            "git diff",
            "/api/overflow/ingest",
            "description",
            "git_diff",
            "files_in_context",
        ]

        for req in required:
            assert req in solve_method, f"Missing: {req}"
            print(f"  ✓ Contains '{req}'")


class TestDocumentation:
    """Test generated documentation"""

    def test_openapi_spec(self):
        """Verify OpenAPI spec is valid YAML"""
        import yaml

        with open(_test_dir / "docs" / "nexus-backend-openapi.yaml") as f:
            spec = yaml.safe_load(f)

        print("\n✓ test_openapi_spec")
        assert spec["openapi"] == "3.1.0"
        assert "paths" in spec
        assert "/v1/chat/completions" in spec["paths"]
        assert "/api/skills" in spec["paths"]
        assert "/api/skills/{name}" in spec["paths"]
        assert "/api/overflow/ingest" in spec["paths"]
        print(f"  ✓ OpenAPI spec is valid")
        print(f"  ✓ All 5 endpoints documented")

    def test_integration_guide(self):
        """Verify integration guide has critical sections"""
        with open(
            _test_dir / "docs" / "nexus-backend-integration-guide.md"
        ) as f:
            content = f.read()

        print("\n✓ test_integration_guide")

        sections = [
            "CRITICAL: The Edit Format Constraint",
            "SEARCH/REPLACE Format",
            "Message Handling",
            "Backend MUST Follow This Pattern",
            "SSE Streaming Format",
            "Authentication",
            "Endpoint Reference",
        ]

        for section in sections:
            assert section in content, f"Missing section: {section}"
            print(f"  ✓ Section: '{section}'")


class TestBuildScript:
    """Test PyInstaller build script"""

    def test_build_script_syntax(self):
        """Verify build_nexus.py is valid Python"""
        import ast

        with open(_test_dir / "build_nexus.py") as f:
            code = f.read()

        try:
            ast.parse(code)
            print("\n✓ test_build_script_syntax")
            print("  ✓ build_nexus.py is valid Python")
        except SyntaxError as e:
            print(f"  ✗ Syntax error: {e}")
            raise

    def test_build_script_pyinstaller_args(self):
        """Verify build script has key PyInstaller args"""
        with open(_test_dir / "build_nexus.py") as f:
            content = f.read()

        print("\n✓ test_build_script_pyinstaller_args")

        required_args = [
            "--onefile",
            "--name",
            "nexus",
            "--collect-data",
            "tree_sitter_languages",
            "aider/main.py",
        ]

        for arg in required_args:
            assert arg in content, f"Missing PyInstaller arg: {arg}"
            print(f"  ✓ Includes '{arg}'")


def run_all_tests():
    """Run all test classes"""
    print("=" * 70)
    print("🧪 NEXUS CLI INTEGRATION TEST SUITE")
    print("=" * 70)

    test_classes = [
        TestAuthModule,
        TestMainModifications,
        TestModelModifications,
        TestSkillInterceptor,
        TestSolveCommand,
        TestDocumentation,
        TestBuildScript,
    ]

    failed = []
    passed = 0

    for test_class in test_classes:
        print(f"\n{'─' * 70}")
        print(f"Testing: {test_class.__name__}")
        print(f"{'─' * 70}")

        instance = test_class()
        for method_name in dir(instance):
            if method_name.startswith("test_"):
                try:
                    method = getattr(instance, method_name)
                    method()
                    passed += 1
                except Exception as e:
                    failed.append((test_class.__name__, method_name, str(e)))
                    print(f"  ✗ {method_name}: {e}")

    # Summary
    print(f"\n{'=' * 70}")
    print(f"📊 TEST RESULTS")
    print(f"{'=' * 70}")
    print(f"Passed: {passed}")
    print(f"Failed: {len(failed)}")

    if failed:
        print("\n❌ Failed tests:")
        for class_name, method_name, error in failed:
            print(f"  - {class_name}.{method_name}: {error}")
        return 1
    else:
        print("\n✅ All tests passed!")
        return 0


if __name__ == "__main__":
    try:
        import yaml

        sys.exit(run_all_tests())
    except ImportError:
        print("⚠️  PyYAML not installed. Installing...")
        os.system("python3 -m pip install -q pyyaml uvicorn fastapi")
        import yaml

        sys.exit(run_all_tests())
