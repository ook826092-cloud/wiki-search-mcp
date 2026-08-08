"""mutmut 3.7 新版测试选择配置（替代废弃的 tests_dir）"""
def pytest_add_cli_args_test_selection(pytest_config):
    return ["test_server.py", "test_fault.py", "test_hypothesis.py"]
