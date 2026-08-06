# 飞书多维表格 + Gemini 卡路里识别

在飞书多维表格里上传食物照片，自动调用 **Gemini** 识别食物并估算卡路里，结果回填到表格。

```
飞书多维表格（图片字段）
   │ ① 飞书自动化流程 Webhook（实时） 或 ② GitHub 定时轮询（兜底）
   ▼
GitHub Actions 运行 main.py
   │ 下载图片 → 调 Gemini → 解析结果
   ▼
回填「卡路里」字段
```

---

## 一、飞书开放平台准备

1. 打开 [飞书开放平台](https://open.feishu.cn) → 创建**企业自建应用**（你的机器人就是它）。
2. **权限管理** → 搜索并开通多维表格的读写权限（如 `bitable:app`、`bitable:app:write`，按页面提示勾选）。
3. **版本管理与发布** → 创建版本并发布（企业应用可能需要管理员审批）。
4. **关键一步**：打开你的多维表格 → 右上角「分享」→ 把**这个应用/机器人添加为协作者**，权限设为「可编辑」。否则应用没有表格访问权限，脚本会报错。
5. 在 **凭证与基础信息** 里拿到 `App ID` 和 `App Secret`。

## 二、拿到表格的 token

打开多维表格，地址栏长这样：

```
https://xxx.feishu.cn/base/bascnXXXXXXXX?table=tblXXXXXXX&view=vewXXXXX
```

- `APP_TOKEN` = `bascnXXXXXXXX`
- `TABLE_ID` = `tblXXXXXXX`

## 三、改字段名（重要）

打开 [main.py](main.py)，在 `FIELD_PAIRS` 里按「图片附件字段 → 结果数字字段 → 餐次称呼」配置：

```python
FIELD_PAIRS = [
    ("午餐图片", "午餐卡路里", "午餐"),   # (图片字段, 结果字段, 提示词里的餐次)
    ("晚餐图片", "晚餐卡路里", "晚餐"),
]
```

- 每个元组第一项是**附件类型**的图片字段，第二项是**数字类型**的结果字段。
- 结果字段存的是**纯数字（千卡）**，方便自动计算——在表格里添加「统计字段 / 汇总」就能算每日总热量。
- 表格里要先建好这些结果字段（如「午餐卡路里」「晚餐卡路里」），脚本只回填**为空**的记录。
- 想加早餐，就多加一行 `("早餐图片", "早餐卡路里", "早餐")`。

## 四、GitHub 侧配置

1. 把本仓库推到 GitHub（如果还没建）：
   ```bash
   git init && git add . && git commit -m "init"
   git remote add origin https://github.com/soiquitmusic/dailyfood.git
   git push -u origin main
   ```
2. 仓库 **Settings → Secrets and variables → Actions** 添加以下 5 个 Secret：

   | Secret 名 | 值 |
   |---|---|
   | `FEISHU_APP_ID` | 飞书应用的 App ID |
   | `FEISHU_APP_SECRET` | 飞书应用的 App Secret |
   | `GEMINI_API_KEY` | Google AI Studio 生成的 Key |
   | `BITABLE_APP_TOKEN` | 表格链接里的 bascnXXX |
   | `BITABLE_TABLE_ID` | 表格链接里的 tblXXX |

3. 去 **Actions** 页手动运行一次 `Feishu-Bitable-Gemini` 验证。在表格里加一行「图片 + 空卡路里」，运行后应能看到回填。

## 五、触发方式：多维表格「按钮字段」手动识别（推荐）

点按钮才识别，手动可控。需要**两个按钮 + 两个自动化流程**。

### 0. 创建 GitHub Token（先做一次）

GitHub 头像 → **Settings → Developer settings → Personal access tokens**，建议用 **Fine-grained token**：仅授权 `soiquitmusic/dailyfood` 仓库，权限勾选 **Actions: Read and write**（用经典 Classic token 则勾 `repo` 即可）。这个 Token 用来让飞书触发 GitHub 的 workflow。

### 1. 加按钮字段

在表格里添加两个**按钮字段**，例如「识别午餐」「识别晚餐」。

### 2. 给每个按钮各建一个自动化流程

以「识别午餐」按钮为例：

1. 点按钮字段 → **设置按钮** → 新建自动化流程
2. 触发条件：**按钮被点击**（选中「识别午餐」这个按钮）
3. 添加节点：**发送 HTTP 请求**
   - 请求方式：`POST`
   - 地址：`https://api.github.com/repos/soiquitmusic/dailyfood/dispatches`
   - 请求头：
     - `Authorization: Bearer <你的 GitHub Token>`
     - `Accept: application/vnd.github+json`
   - 请求体（午餐按钮）：
     ```json
     {"event_type":"feishu_bitable","client_payload":{"meal":"午餐"}}
     ```
4. 「识别晚餐」按钮的自动化流程，请求体把 `meal` 改成 `"晚餐"`。

点哪个按钮，就只识别那一餐、只回填对应的卡路里字段，互不影响。

> ⚠️ 前提：飞书的「发送 HTTP 请求」节点要支持**自定义 Header**（新版支持）。如果不支持，此方案不可用，可退回到自动轮询（见下）。

### 想恢复自动轮询？

把 [gemini-bitable.yml](.github/workflows/gemini-bitable.yml) 里注释掉的 `schedule` 段取消注释即可（GitHub cron 用 UTC 时间）。注意自动模式会处理所有「卡路里为空」的行，与按钮手动模式并存。

---

## 常见问题

- **报错 403 / no permission**：多半是没把应用添加为表格协作者，或权限未发布。
- **图片太大**：Gemini 单次请求上限约 20MB，超出会报错；可以先压缩再传。
- **只识别第一张图**：脚本默认取图片字段的第一张附件，要识别多张需改 `main.py` 里的 `atts[0]` 为循环。
- 仅供个人学习使用，卡路里为 AI 估算值，非营养学结论。
