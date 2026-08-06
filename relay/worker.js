// Cloudflare Worker：飞书按钮 → GitHub Actions 中转
// 解决：飞书「发送 HTTP 请求」节点填请求头太折腾，改用这个中转自动加 token。
//
// 部署后，飞书节点只需：
//   请求方式: POST
//   请求地址: <这个 worker 的地址，如 https://xxx.workers.dev>
//   请求体:   {"event_type":"feishu_bitable","client_payload":{"meal":"晚餐"}}
//   不需要填任何请求头！

export default {
  async fetch(request, env) {
    // 只允许 POST
    if (request.method !== "POST") {
      return new Response("Method Not Allowed", { status: 405 });
    }

    // 读取飞书发来的 JSON（event_type / client_payload）
    const payload = await request.json().catch(() => ({}));

    // 转发给 GitHub，自动补上 Authorization 头
    const resp = await fetch(
      "https://api.github.com/repos/soiquitmusic/dailyfood/dispatches",
      {
        method: "POST",
        headers: {
          Authorization: `Bearer ${env.GITHUB_TOKEN}`,
          Accept: "application/vnd.github+json",
          "Content-Type": "application/json",
        },
        body: JSON.stringify(payload),
      }
    );

    return new Response(await resp.text(), { status: resp.status });
  },
};
