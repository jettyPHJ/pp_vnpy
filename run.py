# run.py
import os

os.environ["LANGUAGE"] = "zh_CN"

import multiprocessing
# 引入 vn.py 核心组件
from vnpy.event import EventEngine
from vnpy.trader.engine import MainEngine
from vnpy.trader.ui import MainWindow, create_qapp

# ==========修改模块================================================
# run.py 和这两个文件夹在同一目录下，Python 会优先从这里加载它们，而不是系统库
from vnpy_ctastrategy import CtaStrategyApp
from vnpy_ctabacktester import CtaBacktesterApp
# ==================================================================

from vnpy_datamanager import DataManagerApp


def main():
    """"""
    # 创建 Qt 应用对象
    qapp = create_qapp()

    # 创建事件引擎
    event_engine = EventEngine()

    # 创建主引擎
    main_engine = MainEngine(event_engine)

    # 添加应用模块 (这里加载的就是你本地的魔改架构)
    main_engine.add_app(CtaStrategyApp)
    main_engine.add_app(CtaBacktesterApp)

    # ====== 把数据管理器装载进引擎 ======
    main_engine.add_app(DataManagerApp)

    # 创建主窗口
    main_window = MainWindow(main_engine, event_engine)
    main_window.showMaximized()

    # 运行 Qt 应用
    qapp.exec()


if __name__ == "__main__":
    # Windows 下多进程运行需要这一步
    multiprocessing.freeze_support()
    main()
