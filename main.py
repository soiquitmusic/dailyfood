# -*- coding: utf-8 -*-
"""飞书多维表格 + Gemini 卡路里识别（由 GitHub Actions 定时运行）

流程：
1. 查找多维表格里「午餐/晚餐卡路里」为空、且对应图片字段有内容的记录
2. 下载图片 → base64 → 发给 Gemini 视觉模型识别
3. 把卡路里分析结果写回对应字段
"""

import os
import re
import sys
import base64
import requests

# ---------- 环境变量（在 GitHub 仓库的 Settings → Secrets 里配置） ----------
FEISHU_APP_ID = os.environ["FEISHU_APP_ID"]
FEISHU_APP_SECRET = os.environ["FEISHU_APP_SECRET"]
GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]
APP_TOKEN = os.environ["BITABLE_APP_TOKEN"]  # 表格链接中 base/ 后面那段 bascnXXXX
TABLE_ID = os.environ["BITABLE_TABLE_ID"]    # 表格链接中 table= 后面那段 tblXXXX

# ---------- 自定义配置：图片字段 → 结果字段 ----------
# 每个元组：(放照片的附件字段名, 结果写回的文本字段名, 提示词里的餐次称呼)
FIELD_PAIRS = [
    ("午餐图片", "午餐卡路里", "午餐"),
    ("晚餐图片", "晚餐卡路里", "晚餐"),
]
MODEL = "gemini-2.5-flash"


def build_prompt(meal):
    return (
        f"请识别这张{meal}图片中的食物并估算总热量。"
        "只回复一个整数，表示这餐的总热量（单位：千卡），不要任何其他文字、单位或符号。"
        "如果图片里没有食物，回复 0。"
    )


def parse_kcal(text):
    """从 Gemini 回复里提取热量数字（千卡）"""
    m = re.search(r"\d+", text or "")
    if not m:
        raise ValueError(f"Gemini 未返回数字: {text!r}")
    return float(m.group())


def get_tenant_token():
    """获取飞书 tenant_access_token（有效期 2 小时）"""
    r = requests.post(
        "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
        json={"app_id": FEISHU_APP_ID, "app_secret": FEISHU_APP_SECRET},
        timeout=15,
    )
    r.raise_for_status()
    return r.json()["tenant_access_token"]


def build_filter():
    """生成筛选条件：任一餐（图片非空 且 结果为空）的记录都要处理"""
    groups = []
    for input_field, output_field, _ in FIELD_PAIRS:
        groups.append(
            {
                "conjunction": "and",
                "conditions": [
                    {"field_name": input_field, "operator": "isNotEmpty", "value": []},
                    {"field_name": output_field, "operator": "isEmpty", "value": []},
                ],
            }
        )
    if len(groups) == 1:
        return groups[0]
    return {"conjunction": "or", "conditions": groups}


def find_pending_records(token):
    """查找待处理记录（自动翻页）"""
    url = (
        f"https://open.feishu.cn/open-apis/bitable/v1/apps/{APP_TOKEN}"
        f"/tables/{TABLE_ID}/records/search"
    )
    headers = {"Authorization": f"Bearer {token}"}
    body = {"page_size": 100, "filter": build_filter()}
    records = []
    page_token = None
    while True:
        payload = dict(body)
        if page_token:
            payload["page_token"] = page_token
        r = requests.post(url, headers=headers, json=payload, timeout=30)
        r.raise_for_status()
        data = r.json().get("data", {})
        records.extend(data.get("items", []))
        page_token = data.get("page_token")
        if not page_token:
            break
    return records


def download_image(att):
    """下载附件图片，返回 (二进制内容, mime_type)"""
    content = None
    # 优先用 tmp_url，失败再用 url
    for key in ("tmp_url", "url"):
        u = att.get(key)
        if not u:
            continue
        try:
            r = requests.get(u, timeout=60)
            if r.status_code == 200 and r.content:
                content = r.content
                break
        except requests.RequestException:
            continue
    if content is None:
        raise RuntimeError("图片下载失败")

    mime = att.get("mime_type") or "image/jpeg"
    return content, mime


def call_gemini(image_bytes, mime_type, prompt):
    """把图片发给 Gemini 视觉模型，返回识别文本"""
    url = (
        f"https://generativelanguage.googleapis.com/v1beta/models/{MODEL}"
        f":generateContent?key={GEMINI_API_KEY}"
    )
    body = {
        "contents": [
            {
                "parts": [
                    {"text": prompt},
                    {
                        "inline_data": {
                            "mime_type": mime_type,
                            "data": base64.b64encode(image_bytes).decode("ascii"),
                        }
                    },
                ]
            }
        ]
    }
    r = requests.post(url, json=body, timeout=120)
    r.raise_for_status()
    return r.json()["candidates"][0]["content"]["parts"][0]["text"]


def update_record(token, record_id, output_field, value):
    """把结果写回多维表格指定字段"""
    url = (
        f"https://open.feishu.cn/open-apis/bitable/v1/apps/{APP_TOKEN}"
        f"/tables/{TABLE_ID}/records/{record_id}"
    )
    headers = {"Authorization": f"Bearer {token}"}
    r = requests.put(
        url, headers=headers, json={"fields": {output_field: value}}, timeout=30
    )
    r.raise_for_status()


def main():
    token = get_tenant_token()
    records = find_pending_records(token)
    print(f"找到 {len(records)} 条待处理记录")

    ok = 0
    for rec in records:
        record_id = rec.get("record_id")
        fields = rec.get("fields") or {}
        for input_field, output_field, meal in FIELD_PAIRS:
            atts = fields.get(input_field) or []
            if not atts:
                continue
            if fields.get(output_field):  # 双保险：结果非空则跳过
                continue
            try:
                image_bytes, mime = download_image(atts[0])
                result = call_gemini(image_bytes, mime, build_prompt(meal))
                kcal = parse_kcal(result)  # 转成数字，方便表格自动求和
                update_record(token, record_id, output_field, kcal)
                ok += 1
                print(f"[OK] {record_id} {input_field} 已回填")
            except Exception as e:
                print(f"[FAIL] {record_id} {input_field} 出错: {e}", file=sys.stderr)

    print(f"完成，成功 {ok} 条")


if __name__ == "__main__":
    main()
