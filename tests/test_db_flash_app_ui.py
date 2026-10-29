# 主应用固化模拟界面入口：复用 Edge 行为验证，隔离端口和假宿主。
import os
import sys
from unittest.mock import patch


def run():
    from tests.test_db_flash_ui import run as run_ui
    with patch.dict(os.environ, {'DB_FLASH_TEST_MAIN_APP': '1',
                                'DB_FLASH_TEST_HTTP_PORT': '8182',
                                'DB_FLASH_TEST_WS_PORT': '8867'}):
        return run_ui()


if __name__ == '__main__':
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    result = run()
    sys.exit(0 if result is not False else 1)
