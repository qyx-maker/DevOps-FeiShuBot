"""项目共享配置加载入口。"""

from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent
ENV_FILE = PROJECT_ROOT / ".env"


def load_project_environment() -> None:
    """从项目根目录加载统一的 .env，保留进程环境变量的优先级。"""
    load_dotenv(dotenv_path=ENV_FILE, override=False)
