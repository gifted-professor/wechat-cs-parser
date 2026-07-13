# 私聊文本 AI 客服工作台 V1

这是一个不包含任何真实聊天记录的独立解析与人工起草项目。数据通过 `--input` 从外部目录传入；运行产生的数据库默认保存在项目自己的 `.wechat-cs/` 目录。V1 仅处理一对一普通文字，不会读取群聊、控制微信或自动发送消息。

输入文件格式见 [DATA_FORMAT.md](DATA_FORMAT.md)。`tests/fixtures/` 只包含虚构测试数据，不包含历史客户信息。

## 当前能力

- 客户机会与未闭环售后队列，所有结论都带规则原因和脱敏证据。
- 客户详情、最近私聊和脱敏身份候选人工复核。
- 200 条角色校准与 500 条风格样本人工审核。
- 手动粘贴最新消息，调用 Kimi 开放平台生成待审核回复。
- 草稿复制、直接采用、修改后采用和放弃反馈。
- 可选的飞书知识缓存注册；动态事实缺少依据时强制转人工。
- 远端 Dashboard 代理，浏览器不会拿到本地服务或 Kimi 密钥。
- M0 只读真值链：四账号会话资格、确定性飞书身份桥，以及顾客付款/退款/换货/补偿规范事实。

## 1. 构建派生数据库

可选：先安装本项目。项目同时兼容现代 `pyproject.toml` 与旧版 `pip/setuptools`：

```bash
python3 -m pip install --no-deps .
```

先准备两个不同的长随机值。不要把它们写进仓库：

```bash
export WECHAT_CS_HMAC_SECRET='至少 32 字符的随机 HMAC 密钥'
export WECHAT_CS_TOKEN='至少 32 字符的随机服务令牌'
python3 -m wechat_cs build \
  --input /absolute/path/to/your-export \
  --db .wechat-cs/data/wechat_cs.sqlite3 \
  --account-id your-account-namespace
python3 -m wechat_cs status --db .wechat-cs/data/wechat_cs.sqlite3
```

构建只读取原始导出，并将脱敏派生数据写入 `.wechat-cs/`。重复构建会保留已有的角色校准、样本审核和草稿反馈。

## 2. 启动本地工作台

```bash
export WECHAT_CS_TOKEN='与上面相同的服务令牌'
export KIMI_API_KEY='Kimi 开放平台 Key'
export KIMI_BASE_URL='https://api.moonshot.cn/v1'
export KIMI_MODEL='kimi-k2.6'
python3 -m wechat_cs.api --host 127.0.0.1 --port 8765
```

打开 `http://127.0.0.1:8765/`。如果配置了服务令牌，在“系统状态”页面输入；令牌只保存在当前浏览器标签页的 `sessionStorage`。

未配置 Kimi Key 时，分析和审核功能仍可用，但起草接口会明确返回不可用，不生成模拟回复。

## 3. 角色校准和样本审核

角色校准必须完成全部 200 条且准确率达到 99%，之后才能安全导出训练集。审核通过的低风险风格样本才会进入起草检索。

```bash
python3 -m wechat_cs export-chatml \
  --db .wechat-cs/data/wechat_cs.sqlite3 \
  --output .wechat-cs/exports/wechat_style_pair.v1.jsonl \
  --split all
```

不要使用 `--include-pending`、`--allow-unverified-roles` 或 `--include-risky` 生成生产训练集；这些参数只用于隔离研究。

## 4. 飞书知识缓存

复制并编辑 `config/knowledge_sources.example.json`。当前服务读取本地缓存文件，不会在请求时直接把整份飞书文档拉入模型。每个缓存需要包含采集时间和可检索条目；缺失或过期会显示在系统状态中。

动态价格、库存、物流、退款或补发问题没有有效依据时，起草结果会带 `grounding_missing`、`needs_clarification` 和 `needs_human`。

## 5. 安装到现有远端 Dashboard

先在 Dashboard 主机上只做兼容性检查：

```bash
python3 dashboard_integration/install.py \
  --dashboard-dir /absolute/path/to/dashboard \
  --project-root /absolute/path/to/wechat-cs-parser \
  --check
```

检查通过后再去掉 `--check` 执行安装。安装器会：

- 复制 `/wechat-cs/` 静态工作台和 `wechat_cs_proxy.js`；
- 给 `server.js` 与 Dashboard 导航做幂等补丁；
- 首次修改前生成 `server.js.before-wechat-cs` 和 `index.html.before-wechat-cs` 备份；
- 绝不复制原始聊天或本地 SQLite 数据库。

Dashboard 服务进程需要：

```bash
export WECHAT_CS_BASE_URL='http://本机的-Tailscale-IP:8765'
export WECHAT_CS_TOKEN='本地服务令牌'
```

浏览器只访问 `/api/wechat-cs/*`。服务令牌由 Dashboard 代理注入，不进入页面脚本。

回滚时停止 Dashboard，恢复两个 `.before-wechat-cs` 备份，并移除 `wechat_cs_proxy.js` 与 `wechat-cs/` 目录。

## 6. 验证

```bash
python3 -m py_compile wechat_cs/*.py
python3 -m unittest discover -s tests -v
node --check wechat_cs/static/app.js
node --check dashboard_integration/wechat_cs_proxy.js
```

真实 Kimi、飞书和远端 Dashboard 验证需要对应的服务端凭据或链接；测试不会读取或打印这些密钥。
