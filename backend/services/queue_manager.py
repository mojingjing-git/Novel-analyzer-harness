"""队列管理
- QueueItem：队列中的一个小说任务（持久化经 _store_path/_load_path 相对路径化，
  整个文件夹搬迁/换盘符/换挂载点后仍能正确恢复）
- QueueManager：队列状态机（增删移推进 / 工作区扫描 / API 预检 / 状态持久化）

自 queue_service.py 拆出（2026-08-31 职责拆分：队列管理与分析编排分离，
分析编排层见 analysis_service 所在的 queue_service.py）。
"""

import json
import logging
import os
import re
import sys
from dataclasses import dataclass, replace
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from typing import List, Optional, Dict, Any

from ..config.settings import AppConfig, ConfigManager
from ..core.llm_client import LLMClient
from ..utils.json_utils import safe_save_json

logger = logging.getLogger(__name__)

# 项目根目录（用户数据落 EXE 同级，与 app.py 策略一致）
# 设计原则（v0.2.0 exe）：所有用户数据（workspace/ config/ queue_state/ log）
# 都在 EXE 同级目录，不在 %APPDATA% 也不在 _MEIPASS。
if getattr(sys, "frozen", False):
    PROJECT_ROOT = Path(sys.executable).resolve().parent
else:
    # 与 app.py 保持一致：优先用 NOVEL_ROOT 指向“真实项目根”（共享盘），
    # 这样队列状态 / 配置 / 工作区数据都落在共享盘，而不是本地副本目录。
    _novel_root = os.environ.get("NOVEL_ROOT")
    PROJECT_ROOT = Path(_novel_root).resolve() if _novel_root else Path(__file__).resolve().parent.parent.parent
QUEUE_STATE_FILE = PROJECT_ROOT / "queue_state.json"
CONFIG_FILE = PROJECT_ROOT / "config.json"


def _store_path(p) -> str:
    """持久化时尽量存「相对 PROJECT_ROOT」的路径，让整个文件夹搬迁 / 换盘符 /
    换挂载点后仍能正确恢复（直接写死绝对路径会在移动后全部失效）。
    仅当路径确实位于 PROJECT_ROOT 之下时才存相对形式，否则退回绝对路径
    （兼容书库外置到其他盘的场景）。"""
    try:
        return Path(p).resolve().relative_to(PROJECT_ROOT).as_posix()
    except Exception:
        return Path(p).as_posix()


def _load_path(s: str) -> Path:
    """读取时：相对路径按 PROJECT_ROOT 还原；绝对路径原样保留
    （兼容旧数据里的 F:/... 等绝对路径）。"""
    p = Path(s)
    if p.is_absolute():
        return p
    return PROJECT_ROOT / p


@dataclass
class QueueItem:
    """队列中的一个小说任务"""
    name: str                           # 书名
    blocks_dir: Path                    # 章节文件目录 (workspace/{书名}/blocks/)
    workspace_dir: Path                 # 工作区目录 (workspace/{书名}/)
    status: str = "pending"             # pending / running / done / failed / skipped
    total_chapters: int = 0
    completed_chapters: int = 0
    block_size: int = 1                 # 当前配置的 block_size（用于恢复时校验）
    start_time: float = 0.0
    end_time: float = 0.0
    error_message: str = ""
    archive_failed: bool = False        # 归档是否失败

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "blocks_dir": _store_path(self.blocks_dir),
            "workspace_dir": _store_path(self.workspace_dir),
            "status": self.status,
            "total_chapters": self.total_chapters,
            "completed_chapters": self.completed_chapters,
            "block_size": self.block_size,
            "start_time": self.start_time,
            "end_time": self.end_time,
            "error_message": self.error_message,
            "archive_failed": self.archive_failed,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "QueueItem":
        return cls(
            name=data.get("name", ""),
            blocks_dir=_load_path(data.get("blocks_dir", "")),
            workspace_dir=_load_path(data.get("workspace_dir", "")),
            status=data.get("status", "pending"),
            total_chapters=data.get("total_chapters", 0),
            completed_chapters=data.get("completed_chapters", 0),
            block_size=data.get("block_size", 1),
            start_time=data.get("start_time", 0.0),
            end_time=data.get("end_time", 0.0),
            error_message=data.get("error_message", ""),
            archive_failed=data.get("archive_failed", False),
        )


class QueueManager:
    """管理小说分析队列（自旧项目原样移植）"""

    def __init__(self):
        self._items: List[QueueItem] = []
        self._current_index: int = -1
        # check_api 的 heal 纠正值经此落盘持久化；由 AnalysisService 注入，
        # 独立构造的实例保持 None（跳过持久化，仅就地纠正）
        self.config_manager: Optional[ConfigManager] = None

    @property
    def items(self) -> List[QueueItem]:
        return self._items

    @property
    def count(self) -> int:
        return len(self._items)

    @property
    def is_empty(self) -> bool:
        return len(self._items) == 0

    @property
    def is_finished(self) -> bool:
        if self.is_empty:
            return True
        return all(item.status in ("done", "skipped", "failed") for item in self._items)

    def add_item(self, item: QueueItem) -> int:
        # 去重：同 blocks_dir 不重复添加
        for existing in self._items:
            if existing.blocks_dir == item.blocks_dir:
                logger.info(f"队列跳过重复: {item.name} (blocks={item.blocks_dir})")
                return self._items.index(existing)
        self._items.append(item)
        logger.info(f"队列添加: {item.name} (blocks={item.blocks_dir})")
        return len(self._items) - 1

    def remove_item(self, index: int) -> Optional[QueueItem]:
        if 0 <= index < len(self._items):
            item = self._items.pop(index)
            if index < self._current_index:
                self._current_index -= 1
            elif index == self._current_index:
                self._current_index = min(self._current_index, len(self._items) - 1)
            logger.info(f"队列移除: {item.name}")
            return item
        return None

    def get_current(self) -> Optional[QueueItem]:
        if self.is_empty:
            return None
        if self._current_index < 0 or self._current_index >= len(self._items):
            self._current_index = self._find_next_pending()
        if 0 <= self._current_index < len(self._items):
            return self._items[self._current_index]
        return None

    def advance(self) -> Optional[QueueItem]:
        if 0 <= self._current_index < len(self._items):
            current = self._items[self._current_index]
            if current.status == "running":
                current.status = "done"
            logger.info(f"队列推进: {current.name} -> {current.status}")

        self._current_index = self._find_next_pending()
        if self._current_index >= 0:
            return self._items[self._current_index]
        return None

    def mark_current(self, status: str, error_message: str = "") -> None:
        if 0 <= self._current_index < len(self._items):
            item = self._items[self._current_index]
            item.status = status
            if error_message:
                item.error_message = error_message
            logger.info(f"队列标记: {item.name} -> {status}")

    def move_up(self, index: int) -> bool:
        if 0 < index < len(self._items):
            self._items[index], self._items[index - 1] = self._items[index - 1], self._items[index]
            if self._current_index == index:
                self._current_index -= 1
            elif self._current_index == index - 1:
                self._current_index += 1
            return True
        return False

    def move_down(self, index: int) -> bool:
        if 0 <= index < len(self._items) - 1:
            self._items[index], self._items[index + 1] = self._items[index + 1], self._items[index]
            if self._current_index == index:
                self._current_index += 1
            elif self._current_index == index + 1:
                self._current_index -= 1
            return True
        return False

    def clear(self) -> None:
        self._items.clear()
        self._current_index = -1
        logger.info("队列已清空")

    def get_progress(self) -> Dict[str, Any]:
        completed_items = sum(1 for item in self._items if item.status == "done")
        current = self.get_current()
        # 单位统一为「块」：completed_chapters 是已完成的「块」数，total_chapters 是「章」数
        # （等于 blocks 目录下 N.txt 文件个数）。REST 兜底进度条必须与 WS 路径同单位，
        # 因此 total 也要换算成块数 ceil(章 / block_size)，否则把「块/章」混算后百分比严重失真。
        if current:
            bs = max(1, current.block_size)
            current_progress = current.completed_chapters
            current_total = (current.total_chapters + bs - 1) // bs
        else:
            current_progress = 0
            current_total = 0
        return {
            "total_items": len(self._items),
            "completed_items": completed_items,
            "current_item": current.name if current else "",
            "current_progress": current_progress,
            "current_total": current_total,
        }

    def scan_directory(self, base_dir: Path) -> List[QueueItem]:
        """扫描 {书名}/blocks/ 结构（网络盘优化：scandir + 线程池并行列举）"""
        base = Path(base_dir)
        if not base.is_dir():
            return []

        # 1) 顶层目录枚举：os.scandir 复用 d_type，避免逐目录额外 stat
        #    （网络盘下每次 stat 都是一次到服务器的往返，能省则省）
        subdirs = []
        with os.scandir(base) as it:
            for entry in it:
                if entry.is_dir():
                    subdirs.append(Path(entry.path))

        # 2) 每本书的 blocks/*.txt 列举并行化，把多本书的网络延迟重叠掉
        def _scan_one(subdir: Path) -> Optional[QueueItem]:
            blocks = subdir / "blocks"
            if not blocks.is_dir():
                return None
            txt_files = [f for f in blocks.glob("*.txt") if re.match(r"^\d+\.txt$", f.name)]
            if not txt_files:
                return None
            return QueueItem(
                name=subdir.name,
                blocks_dir=blocks,
                workspace_dir=subdir,
                total_chapters=len(txt_files),
            )

        items: List[QueueItem] = []
        if subdirs:
            max_workers = min(32, (os.cpu_count() or 4) + 4)
            with ThreadPoolExecutor(max_workers=max_workers) as ex:
                for it in ex.map(_scan_one, subdirs):
                    if it is not None:
                        items.append(it)

        items.sort(key=lambda x: x.name)
        for it in items:
            logger.info(f"扫描到小说: {it.name} ({it.total_chapters}章)")
        return items

    def validate_item(self, item: QueueItem) -> bool:
        """校验 blocks 目录是否存在且有 txt 文件"""
        if not item.blocks_dir.exists():
            return False
        txt_files = [f for f in item.blocks_dir.glob("*.txt") if re.match(r"^\d+\.txt$", f.name)]
        return len(txt_files) >= 1

    async def check_api(self, config: AppConfig) -> bool:
        """轻量 API 预检查：优先用 /models 健康检查端点（不消耗对话 token、不污染日志），
        仅在 /models 不可用或返回空时回退到一次极短 chat 探针。
        原生协议两种探针均失败时，再以翻转 provider 各探一次（P2 2026-08-24：
        聚合网关常以 OpenAI 协议暴露 claude-* 模型，detect_provider 会误判为
        anthropic → /v1/messages 404 → auto 模式下整本书无提示全量失败）。"""
        # 空模型名直接拦截：带着空 model 发起请求只会得到晦涩的 API 报错
        if not str(config.api.model or '').strip():
            logger.warning("API 预检查失败：未配置模型（api.model 为空），请在设置页填写模型名")
            return False

        flipped = "openai" if (config.api.provider or "auto") in ("auto", "anthropic") else "anthropic"

        async def models_ok(provider: str) -> bool:
            try:
                models = await LLMClient.list_models(config.api.base_url, config.api.api_key, provider)
                return bool(models)
            except Exception as e:
                logger.debug(f"models 健康检查异常({provider}): {e}")
                return False

        async def chat_ok(provider: str) -> bool:
            try:
                client = LLMClient(replace(config.api, provider=provider))
                probe = await client.chat([{"role": "user", "content": "hi"}])
                if probe.success:
                    return True
                logger.debug(f"chat 探针失败({provider}): {probe.error}")
                return False
            except Exception as e:
                logger.debug(f"chat 探针异常({provider}): {e}")
                return False

        native = (config.api.provider or "auto")

        # ① 原生协议 models → ② 原生 chat 探针
        if await models_ok(native):
            # 终审 M1 收口：/models 在 OpenAI 面成功且模型为 claude-* 时，
            # 即网关以 OpenAI 协议暴露 claude 系列——detect_provider 的前缀规则
            # 会把运行期请求误导向 /v1/messages（整书 404）。就地纠正并指引持久化。
            # 复审 Critical 护栏：官方 Anthropic 直连（base_url 含 anthropic）时
            # auto 解析本就命中 Anthropic 面 /models，探针成功≠协议误判，
            # 不得改写为 openai。仅聚合网关（URL 无 anthropic 特征）才纠正。
            if (native == "auto"
                    and str(config.api.model or "").lower().startswith("claude-")
                    and "anthropic" not in (config.api.base_url or "").lower()):
                config.api.provider = "openai"
                # 权威传播：config_manager 的内存单例 + 磁盘落盘。
                # 否则自动总结/手动总结经 config_manager.load() 重读磁盘时，
                # 纠正值丢失（复审 Important #2）。探针已验证该值可用，落盘安全；
                # save 内置 env-key 不回写保护，不会泄露密钥。
                try:
                    cm = getattr(self, "config_manager", None)
                    if cm is not None:
                        cm.config.api.provider = "openai"
                        cm.save()
                except Exception as e:
                    logger.warning(f"provider 纠正值持久化失败（本次运行仍生效）: {e}")
                logger.warning(
                    "检测到 OpenAI 兼容网关以 claude-* 模型提供服务：已就地纠正 "
                    "provider=openai 并写入配置持久化，"
                    "避免运行期请求被前缀规则误导向 /v1/messages。")
            return True
        logger.debug("models 健康检查不可用或为空，回退 chat 探针")
        if await chat_ok(native):
            return True

        # ③ 跨协议探针：翻转 provider 各试一次 models + chat，
        # 任一成功即认定协议误判——就地纠正（运行期对象，随后所有服务共用此
        # 配置对象即自动生效），并指引持久化；全败不改写。
        logger.warning(f"原生协议({native})探针均失败，尝试跨协议({flipped})探针…")
        if await models_ok(flipped) or await chat_ok(flipped):
            config.api.provider = flipped
            logger.warning(
                f"检测到协议误判：已就地纠正 provider={flipped}（本次运行生效）。"
                f"请在设置页保存配置以持久化，避免每次启动重复探测。")
            return True

        logger.warning("API 预检查失败：原生与跨协议探针均失败")
        return False

    def save_state(self, path: Path) -> None:
        """持久化队列状态（原子写，避免断电损坏 queue_state.json 丢失队列）"""
        state = {
            "current_index": self._current_index,
            "items": [item.to_dict() for item in self._items],
        }
        if safe_save_json(state, path, ensure_ascii=False):
            logger.info(f"队列状态已保存: {path}")
        else:
            logger.error(f"队列状态保存失败: {path}")

    def load_state(self, path: Path) -> None:
        """恢复队列状态"""
        if not path.exists():
            return
        try:
            state = json.loads(path.read_text(encoding="utf-8"))
            self._items = [QueueItem.from_dict(d) for d in state.get("items", [])]
            self._current_index = state.get("current_index", -1)
            # 上次异常退出遗留的 running 状态重置为 pending（可续跑）
            for item in self._items:
                if item.status == "running":
                    item.status = "pending"
            logger.info(f"队列状态已恢复: {len(self._items)}项")
        except Exception as e:
            logger.error(f"队列状态恢复失败: {e}")

    def infer_progress(self, item: QueueItem) -> int:
        """扫描 output 目录推断已完成章数，校验 block_size"""
        output_dir = item.workspace_dir / "output"
        if not output_dir.exists():
            return 0
        valid_count = 0
        for f in output_dir.glob("*_result.json"):
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
                if data.get("block_size", 0) == item.block_size:
                    valid_count += 1
            except Exception:
                continue
        return valid_count

    def _find_next_pending(self) -> int:
        for i, item in enumerate(self._items):
            if item.status == "pending":
                return i
        return -1
