# -*- coding: utf-8 -*-
"""飞书多维表格 + Gemini 卡路里识别（由 GitHub Actions 运行）

流程：
1. 拉取多维表格所有记录，在代码里筛选待处理的（图片有内容、结果为空）
2. 下载图片 → base64 → 发给 Gemini 视觉模型识别
3. 把卡路里分析结果（数字）写回对应字段

可选环境变量 MEAL：只处理指定餐次（如 "午餐"），用于飞书按钮字段触发。
"""

import os
import re
import sys
import time
import base64
import requests

# ---------- 环境变量（在 GitHub 仓库的 Settings → Secrets 里配置） ----------
FEISHU_APP_ID = os.environ["FEISHU_APP_ID"]
FEISHU_APP_SECRET = os.environ["FEISHU_APP_SECRET"]
GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]
APP_TOKEN = os.environ["BITABLE_APP_TOKEN"]  # 表格链接中 base/ 后面那段（bascn 或类似）
TABLE_ID = os.environ["BITABLE_TABLE_ID"]    # 表格链接中 table= 后面那段 tblXXXX

# ---------- 自定义配置：图片字段 → 结果字段 ----------
# 每个元组：(放照片的附件字段名, 结果写回的字段名, 提示词里的餐次称呼)
FIELD_PAIRS = [
    ("午餐图片", "午餐卡路里", "午餐"),
    ("晚餐图片", "晚餐卡路里", "晚餐"),
]
MODEL = "gemini-2.5-flash"


def active_pairs():
    """本次要处理的字段组；设置了 MEAL 环境变量时只处理对应餐次"""
    meal = os.environ.get("MEAL", "").strip()
    if meal:
        return [p for p in FIELD_PAIRS if p[2] == meal]
    return FIELD_PAIRS


def api_request(method, url, token=None, **kwargs):
    """发请求，出错时把飞书返回的错误信息带出来"""
    headers = kwargs.pop("headers", {})
    if token:
        headers["Authorization"] = f"Bearer {token}"
    r = requests.request(method, url, headers=headers, timeout=60, **kwargs)
    if not r.ok:
        raise RuntimeError(f"飞书接口 {r.status_code}: {r.text[:500]}")
    return r


def get_tenant_token():
    """获取飞书 tenant_access_token（有效期 2 小时）"""
    r = requests.post(
        "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
        json={"app_id": FEISHU_APP_ID, "app_secret": FEISHU_APP_SECRET},
        timeout=15,
    )
    r.raise_for_status()
    return r.json()["tenant_access_token"]


def get_table_fields(token):
    """列出表格的所有字段名（用于核对配置）"""
    url = (
        f"https://open.feishu.cn/open-apis/bitable/v1/apps/{APP_TOKEN}"
        f"/tables/{TABLE_ID}/fields"
    )
    r = api_request("GET", url, token)
    return [f.get("field_name") for f in r.json().get("data", {}).get("items", [])]


def find_pending_records(token):
    """拉取所有记录，在代码里筛选：任一餐（图片非空 且 结果为空）"""
    url = (
        f"https://open.feishu.cn/open-apis/bitable/v1/apps/{APP_TOKEN}"
        f"/tables/{TABLE_ID}/records/search"
    )
    records = []
    page_token = None
    while True:
        body = {"page_size": 100}
        if page_token:
            body["page_token"] = page_token
        r = api_request("POST", url, token, json=body)
        data = r.json().get("data", {})
        records.extend(data.get("items", []))
        page_token = data.get("page_token")
        if not page_token:
            break

    pending = []
    pairs = active_pairs()
    for rec in records:
        fields = rec.get("fields") or {}
        for input_field, output_field, _ in pairs:
            # 图片有内容 且 结果字段为空（None 或空字符串）
            if fields.get(input_field) and fields.get(output_field) in (None, ""):
                pending.append(rec)
                break
    return pending


def download_image(token, att):
    """下载附件图片，返回 (二进制内容, mime_type)"""
    print("附件结构:", att)  # 调试：看附件里有哪些字段
    auth = {"Authorization": f"Bearer {token}"}

    # 方式1：附件自带的下载地址是飞书 API，需要带租户 token
    for u in (att.get("url"), att.get("tmp_url")):
        if not u:
            continue
        try:
            r = requests.get(u, headers=auth, timeout=60)
            if r.status_code == 200 and r.content:
                return r.content, att.get("mime_type") or "image/jpeg"
        except requests.RequestException:
            continue

    # 方式2：兜底，用云空间 medias 下载接口（需 file_token + 租户 token）
    file_token = att.get("file_token")
    if file_token:
        url = f"https://open.feishu.cn/open-apis/drive/v1/medias/{file_token}/download"
        r = api_request("GET", url, token)
        mime = (r.headers.get("Content-Type") or "").split(";")[0] or "image/jpeg"
        return r.content, mime

    raise RuntimeError("图片下载失败：附件里没有可用的下载地址")


def call_gemini(image_bytes, mime_type, prompt, retries=4):
    """把图片发给 Gemini 视觉模型，返回识别文本（带重试）"""
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
    last_err = None
    for attempt in range(retries):
        r = requests.post(url, json=body, timeout=120)
        if r.ok:
            try:
                return r.json()["candidates"][0]["content"]["parts"][0]["text"]
            except Exception:
                raise RuntimeError(f"Gemini 响应解析失败: {r.text[:300]}")
        last_err = f"Gemini {r.status_code}: {r.text[:500]}"
        print(f"[重试 {attempt + 1}/{retries}] {last_err}")
        time.sleep(3 * (attempt + 1))
    raise RuntimeError(last_err)


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


def update_record(token, record_id, output_field, value):
    """把结果写回多维表格指定字段"""
    url = (
        f"https://open.feishu.cn/open-apis/bitable/v1/apps/{APP_TOKEN}"
        f"/tables/{TABLE_ID}/records/{record_id}"
    )
    api_request("PUT", url, token, json={"fields": {output_field: value}})


def main():
    token = get_tenant_token()

    # 打印表格现有字段，核对配置
    field_names = get_table_fields(token)
    print("表格现有字段:", field_names)
    for input_field, output_field, meal in FIELD_PAIRS:
        for f in (input_field, output_field):
            if f not in field_names:
                print(
                    f"[警告] 配置的字段「{f}」在表格里不存在！"
                    f"请核对 main.py 的 FIELD_PAIRS 或去表格建这个字段。",
                    file=sys.stderr,
                )

    records = find_pending_records(token)
    print(f"找到 {len(records)} 条待处理记录")

    ok = 0
    for rec in records:
        record_id = rec.get("record_id")
        fields = rec.get("fields") or {}
        for input_field, output_field, meal in active_pairs():
            atts = fields.get(input_field) or []
            if not atts:
                continue
            if fields.get(output_field) not in (None, ""):
                continue
            try:
                image_bytes, mime = download_image(token, atts[0])
                result = call_gemini(image_bytes, mime, build_prompt(meal))
                kcal = parse_kcal(result)  # 转成数字，方便表格自动求和
                update_record(token, record_id, output_field, kcal)
                ok += 1
                print(f"[OK] {record_id} {input_field} 已回填 {kcal} 千卡")
            except Exception as e:
                print(f"[FAIL] {record_id} {input_field} 出错: {e}", file=sys.stderr)

    print(f"完成，成功 {ok} 条")


if __name__ == "__main__":
    main()
