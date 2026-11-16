# 许可证说明（LICENSE-NOTICE）

## 一、本项目是什么

`Hermes` 是 [MediaCrawler](https://github.com/NanmiCoder/MediaCrawler) 的**封装层 + 补丁集合**。

它**不包含** MediaCrawler 的源代码。仓库里的 `patches/` 目录存放的是「对 MediaCrawler 源代码的修改描述」（`.patch` 文件），`newfiles/` 目录存放的是原项目中不存在的少量新增文件（命令行封装、MCP 服务、文档）。

安装时，`install.ps1` 会从官方仓库下载 MediaCrawler，再把上述补丁应用上去。

---

## 二、MediaCrawler 的许可证

MediaCrawler 使用 **NON-COMMERCIAL LEARNING LICENSE 1.1**（非商业学习使用许可 1.1）。

原始版权声明：

```
NON-COMMERCIAL LEARNING LICENSE 1.1

Copyright (c) [2024] [relakkes@gmail.com]
```

完整许可文本见：https://github.com/NanmiCoder/MediaCrawler/blob/main/LICENSE

**核心条款摘要**（摘要仅供参考，以原文为准）：

| 条款 | 内容 |
|---|---|
| 授权范围 | 免费、非排他、不可转让地使用、复制、修改、合并，**仅限非商业学习目的** |
| 商用 | **未经版权人书面同意，不得商用** |
| 分发 | 必须一并保留版权声明和许可文本 |
| 规模 | 不得用于大规模爬取 |

---

## 三、因此，本项目的许可状态

1. **本仓库内容（补丁 + 封装层）同样仅限非商业学习研究使用，禁止商用。**
2. 补丁是对他人源代码的修改描述，属于衍生作品，须遵守 MediaCrawler 的许可证。
3. 分发本项目时，**必须**同时保留：
   - 本文件（`LICENSE-NOTICE.md`）
   - MediaCrawler 的原始许可证全文（安装后位于 `<安装目录>\LICENSE`）
   - 上述版权声明

---

## 四、使用者须知

使用本项目（含其调用 MediaCrawler 采集数据的行为）时：

1. **遵守目标平台的服务条款与 `robots.txt`**。
2. 不得采集、存储、传播他人隐私数据；不得用于骚扰、诈骗、舆论操纵等用途。
3. 不得用于商业目的（包括但不限于：付费数据服务、商业竞品分析、对外售卖数据集）。
4. 不得进行大规模、高频次爬取，避免对目标平台造成负担。
5. **一切法律后果由使用者自行承担**，本项目作者不承担任何责任。

---

## 五、如果你要商用

需要**直接联系 MediaCrawler 原作者**取得书面授权：

- 项目地址：https://github.com/NanmiCoder/MediaCrawler
- 作者邮箱见原始 LICENSE 文件：`relakkes@gmail.com`

**在拿到书面同意之前，不要将本项目用于任何商业场景，也不要上架到商业插件市场。**

---

## 六、免责声明

本项目按「现状」提供，不附带任何明示或暗示的担保。作者不对以下情况负责：

- 因使用本项目导致的任何直接或间接损失
- 账号被封禁、限流、数据被删除
- 因目标平台反爬策略变更导致的功能失效
- 因使用者违反法律法规或平台条款而产生的任何后果
