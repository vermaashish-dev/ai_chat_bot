from scripts.package_submission import collect, secret_values


def test_package_excludes_data_db_env_and_caches(tmp_path):
    for rel in ["README.md", ".env", ".env.example", "persona.db", "data/sellers.csv", "data/PS07_DATA_DICTIONARY.md",
                "app/main.py", "app/__pycache__/main.cpython-310.pyc", "outputs/switch_log.md", ".venv/lib/x.py"]:
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("x", encoding="utf-8")
    got = {p.relative_to(tmp_path).as_posix() for p in collect(tmp_path)}
    assert got == {"README.md", ".env.example", "data/PS07_DATA_DICTIONARY.md", "app/main.py", "outputs/switch_log.md"}


def test_secret_values_reads_only_secret_like_settings(tmp_path):
    env = tmp_path / ".env"
    env.write_text("API_TOKEN=vani_abcdefgh123\nOPENAI_API_KEY=\nLLM_MODEL=gpt-4.1-mini-long\nANTHROPIC_WORKSPACE_ID=wrkspc_12345678\n", encoding="utf-8")
    assert secret_values(env) == ["vani_abcdefgh123", "wrkspc_12345678"]
