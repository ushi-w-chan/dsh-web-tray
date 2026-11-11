# dsh-web-tray

把 DeepSeek Harness 的 Web UI 做成一个可从启动器（Super/Win + Space）搜索、
启动后常驻系统托盘的小应用。启动时在后台运行 `dsh web`，托盘菜单可以打开
Web 界面、复制地址、重启/停止服务、查看日志、退出（退出会停掉它启动的服务）。

Debian + Wayland 优先（已在 niri + DMS 上验证），KDE / GNOME + AppIndicator 扩展 /
Waybar 的 tray 模块同样适用。

## 安装

```bash
git clone https://github.com/ushi-w-chan/dsh-web-tray.git
cd dsh-web-tray
python3 install.py            # 需要 sudo 装依赖 + 写 /usr/local 图标
python3 install.py --start    # 或直接从启动器搜 DeepSeek Harness
```

没有 git 也可以：下载 zip 解压后 `python3 install.py`。

常用参数：

| 参数 | 作用 |
| --- | --- |
| `--port 3081` | 换监听端口（默认 3080） |
| `--start` | 装完立即启动 |
| `--autostart` | 登录后自动启动 |
| `--no-apt` | 跳过 apt 装包（依赖已就绪） |
| `--no-system-icons` | 不写 `/usr/local/share/icons`（启动器图标会退化成字母） |
| `--uninstall [--purge]` | 卸载（`--purge` 同时删配置） |

## 它能做什么

- 托盘右键：打开 Web 界面、复制访问地址、重启服务、停止服务、接管服务并重启、
  打开日志、退出。
- 首次访问必须带 token（`http://127.0.0.1:3080/?token=...`），直接访问是 401；
  应用会从日志里学出带 token 的地址，"打开 Web 界面"用的就是它。
- 端口被别的 `dsh web` 占用时以"外部实例"模式附着，不重复起服务；菜单里的
  "接管服务并重启"会先停掉占用端口的 dsh 进程再自己启动。
- 服务异常退出会自动重启（180 秒内最多 5 次）。
- 命令行：`dsh-web-tray`、`--status`、`--open`、`--stop`。

## 安装脚本做了什么

1. `apt-get install python3-gi gir1.2-gtk-3.0 gir1.2-ayatanaappindicator3-0.1 librsvg2-bin iproute2`
2. 应用源码 → `~/.local/share/dsh-web-tray/dsh_web_tray.py`
3. 鲸鱼图标 → `~/.local/share/icons/hicolor/...`（GTK）与
   `/usr/local/share/icons/hicolor/...`（Qt/DMS 只扫系统目录）
4. 启动脚本 → `~/.local/bin/dsh-web-tray`
5. 启动器条目 → `~/.local/share/applications/dsh-web.desktop`
6. 默认配置 → `~/.config/dsh-web-tray/config.json`（已存在不覆盖）
7. 刷新桌面数据库与图标缓存，并自检 python 绑定 / dsh / 托盘宿主

所有路径基于 `$HOME` 与 `XDG_*`，脚本可重复执行（幂等）。

## 配置

`~/.config/dsh-web-tray/config.json`：

```json
{
  "port": 3080,
  "host": "127.0.0.1",
  "command": "",
  "auto_restart": true,
  "open_browser_on_launch": false,
  "extra_args": []
}
```

## 前提

- Debian 12/13（其他发行版需自行安装 GTK3 + AyatanaAppIndicator 的 Python 绑定）。
- 桌面提供 StatusNotifierItem host（托盘）：KDE 自带、GNOME 需 AppIndicator 扩展、
  DMS/quickshell、Waybar 的 `tray` 模块均可。
- Node.js（nvm 或 apt）+ `npm i -g @deepseek-ai/dsh`；没有时退回
  `npx -y @deepseek-ai/dsh web`（首次较慢）。

## 排错

- **启动器显示字母而不是鲸鱼**：Qt 不扫描 `~/.local/share/icons`，图标必须装到
  `/usr/local/share/icons/hicolor`（`--no-system-icons` 会跳过）。
- **托盘显示首字母**：SNI 的 `IconName` 必须导出为绝对文件路径，且不要设置
  `IconThemePath`，否则 DMS/quickshell 解析失败。
- **换了图标栏里不变**：托盘宿主按 URL 缓存图片，换图标要同时换文件名。
- 启动时 `libayatana-appindicator is deprecated` 警告无害。
- 日志：`~/.local/state/dsh-web-tray/server.log`。

## 图标

托盘图标是 DeepSeek 的鲸鱼应用图标，取自 Wikimedia Commons 的
[Deepseek-logo-icon.svg](https://commons.wikimedia.org/wiki/File:Deepseek-logo-icon.svg)，
仅用于标识本工具管理的 DeepSeek Harness 服务；商标归 DeepSeek 所有。

## License

MIT（见 [LICENSE](LICENSE)）。
