# -*- coding: utf-8 -*-
# Copyright (c) 2025 relakkes@gmail.com
#
# This file is part of MediaCrawler project.
#
# 声明：本代码仅供学习和研究目的使用。

"""
额外导出 Excel
==============

词云图只能基于 json/jsonl 里已落盘的数据生成（见 tools/async_file_writer.py），
所以一旦把保存格式选成 excel，就没法再出词云图。

开启 config.ENABLE_EXCEL_EXPORT 之后：主保存格式仍然是 json/jsonl（词云正常生成），
爬取结束时再把落盘的数据读回来，额外导出一份 Excel，两者兼得。

复用 store.excel_store_base.ExcelStoreBase —— 它按 item.keys() 生成表头，
与 jsonl 里的字段完全一致，因此导出的工作簿与「直接选 excel 格式」等价
（Contents / Comments / Creators 多 sheet，带表头样式与自适应列宽）。
"""

import csv
import json
import os
from typing import Any, Dict, List, Tuple

import aiofiles

import config
from tools import utils
from tools.time_util import get_current_date
from var import crawler_type_var

# item_type -> ExcelStoreBase 上的写入方法
_ITEM_SINKS: Dict[str, str] = {
    "contents": "store_content",
    "comments": "store_comment",
    "creators": "store_creator",
    "dynamics": "store_dynamic",
    "contacts": "store_contact",
}

# 可以回读的落盘格式；数据库类格式无法回读，直接跳过
_SUPPORTED_SAVE_OPTIONS = ("json", "jsonl", "csv")


def _data_base_dir() -> str:
    if config.SAVE_DATA_PATH:
        return f"{config.SAVE_DATA_PATH}/{config.PLATFORM}"
    return f"data/{config.PLATFORM}"


def _candidate_files(item_type: str, crawler_type: str) -> List[Tuple[str, str]]:
    """按优先级返回 [(路径, 格式)]：jsonl > json > csv。"""
    base = _data_base_dir()
    date = get_current_date()
    stem = f"{crawler_type}_{item_type}_{date}"
    return [
        (f"{base}/jsonl/{stem}.jsonl", "jsonl"),
        (f"{base}/json/{stem}.json", "json"),
        (f"{base}/csv/{stem}.csv", "csv"),
    ]


async def _read_records(path: str, kind: str) -> List[Dict[str, Any]]:
    """读取落盘数据，统一返回 dict 列表。"""
    if kind == "jsonl":
        records: List[Dict[str, Any]] = []
        async with aiofiles.open(path, "r", encoding="utf-8") as f:
            async for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    item = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(item, dict):
                    records.append(item)
        return records

    async with aiofiles.open(path, "r", encoding="utf-8-sig", newline="") as f:
        content = await f.read()

    if kind == "json":
        if not content.strip():
            return []
        data = json.loads(content)
        if isinstance(data, dict):
            return [data]
        return [item for item in data if isinstance(item, dict)]

    records = []
    for row in csv.DictReader(content.splitlines()):
        records.append({k: v for k, v in row.items() if k is not None})
    return records


async def export_excel_from_saved_files() -> None:
    """把已落盘的 JSON/JSONL/CSV 数据额外导出一份 Excel。

    任何异常都只记日志，绝不影响爬虫已经拿到的结果。
    """
    if not getattr(config, "ENABLE_EXCEL_EXPORT", False):
        return

    if config.SAVE_DATA_OPTION == "excel":
        utils.logger.info("[ExcelExport] 主保存格式已是 excel，无需额外导出")
        return

    if config.SAVE_DATA_OPTION not in _SUPPORTED_SAVE_OPTIONS:
        utils.logger.warning(
            f"[ExcelExport] 保存格式 {config.SAVE_DATA_OPTION} 不支持附加 Excel 导出（仅支持 "
            f"{'/'.join(_SUPPORTED_SAVE_OPTIONS)}），已跳过"
        )
        return

    try:
        from store.excel_store_base import ExcelStoreBase
    except Exception as e:
        utils.logger.error(f"[ExcelExport] 加载 Excel 导出组件失败: {e}")
        return

    crawler_type = crawler_type_var.get() or config.CRAWLER_TYPE

    try:
        store = ExcelStoreBase.get_instance(config.PLATFORM, crawler_type)
    except Exception as e:
        utils.logger.error(f"[ExcelExport] 初始化 Excel 导出失败（是否缺少 openpyxl？）: {e}")
        return

    total = 0
    for item_type, sink_name in _ITEM_SINKS.items():
        sink = getattr(store, sink_name, None)
        if sink is None:
            continue

        source_path = ""
        records: List[Dict[str, Any]] = []
        for path, kind in _candidate_files(item_type, crawler_type):
            if os.path.exists(path) and os.path.getsize(path) > 0:
                try:
                    records = await _read_records(path, kind)
                except Exception as e:
                    utils.logger.error(f"[ExcelExport] 读取 {path} 失败: {e}")
                    records = []
                if records:
                    source_path = path
                    break

        if not records:
            continue

        for record in records:
            try:
                await sink(record)
            except Exception as e:
                utils.logger.error(f"[ExcelExport] 写入 {item_type} 失败: {e}")
        total += len(records)
        utils.logger.info(f"[ExcelExport] {item_type}: 从 {source_path} 导入 {len(records)} 条")

    if total == 0:
        utils.logger.info("[ExcelExport] 没有可导出的数据，跳过生成 Excel")
        return

    ExcelStoreBase.flush_all()
    utils.logger.info(f"[ExcelExport] 额外 Excel 导出完成，共 {total} 条")
