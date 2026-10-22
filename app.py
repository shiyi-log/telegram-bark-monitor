"""兼容原单文件启动方式的入口。"""

from bark_monitor.app import main, run

__all__ = ["main", "run"]


if __name__ == "__main__":
    raise SystemExit(main())
