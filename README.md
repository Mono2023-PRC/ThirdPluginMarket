# ThirdPluginMarket

基于 `FastAPI` 和 `uvicorn` 的 ToolDelta 第三方插件市场下载站。

## 功能

- 同时提供官方插件市场和第三方插件市场的合并索引。
- 第三方插件在下载时动态改写：
  - `plugin-id` 追加 `-third`
  - 插件目录名对应的 `name = "..."` 追加 `-third`
  - `pre-plugins` / 整合包 `plugin-ids` 自动映射到追加后缀的第三方插件 ID
- 官方仓库支持定时检测 `git` 更新并自动拉取。
- 第三方目录支持定时扫描并重建市场索引。
- 日志包含时间、访问 IP、请求仓库、请求文件、请求方式、IP 累计请求次数。
- 日志按 `年-月-日.log` 保存。
- 路径解析带边界校验，避免通过 `..` 等路径穿越访问仓库外文件。
- 支持 Windows 和 Linux 路径。

## 配置

配置文件为 [config.json](./config.json)。

关键字段：

- `markets.official_root`: 官方插件市场仓库目录
- `markets.third_party_root`: 第三方插件市场目录
- `markets.greetings`: 合并后 `market_tree.json` 的 `Greetings`
- `sync.official_check_interval_seconds`: 官方仓库检测周期
- `sync.third_party_scan_interval_seconds`: 第三方目录扫描周期
- `server.port`: 服务端口，默认 `24011`

## 运行

```bash
uv run python main.py
```

启动后可访问：

- `/market_tree.json`
- `/plugin_ids_map.json`
- `/latest_versions.json`
- `/directory_tree.json`
- `/directory.json`
- `/healthz`

构建产物会写入 `build/`，访问日志会写入 `logs/`。
