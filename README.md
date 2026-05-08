# meme-cli

批量把普通图片转换成更适合微信发送/添加表情的素材。

## 功能

- 单图或目录批量处理
- 支持 `png/jpg/jpeg/bmp/gif/webp`
- 等比缩放到指定长边
- 输出 `gif/png/auto`
- `--dedupe` 基于 SHA256 去重
- `--max-bytes` 自动缩小到目标体积附近
- `--wechat-safe` 套用更保守的微信友好参数
- 生成 `manifest.json`
- 可选生成 `manifest.csv`
- 生成 `summary.json`
- 失败项输出 `failures.json`

## 用法

```powershell
python -m meme_cli.cli convert input_dir output_dir --mode 2 --size 200
python -m meme_cli.cli convert a.png out --format png --size raw
python -m meme_cli.cli convert input_dir output_dir --format auto --dedupe --wechat-safe --manifest-csv
python -m meme_cli.cli sync-scan sync_report_dir --download-cdn
python -m meme_cli.cli split-sheet sheet.png out --rows 3 --cols 3 --size 200 --format gif --transparent-bg
```

## 参数说明

- `--mode 1|2`
  - 1: default/adaptive palette
  - 2: fast octree palette
- `--size raw|N`
  - `raw` 保持原始尺寸
  - `N` 表示长边缩放到 N
- `--format gif|png|auto`
  - `auto`:
    - 动图输入输出为 `gif`
    - 静图在 `--wechat-safe` 下优先 `gif`
    - 其他静图优先 `png`
- `--keep-gif`
  - 输入本来就是 GIF，且不需要缩放/限体积时直接复制
- `--dedupe`
  - 对同一批输入按源文件 SHA256 去重
- `--max-bytes`
  - 若输出超出限制，会自动逐步缩小
- `--wechat-safe`
  - 若未显式指定：
    - `max-bytes=512KB`
  - 且在 `--format auto` 时静图优先输出 `gif`
- `--manifest-csv`
  - 额外输出 `manifest.csv`

## 输出文件

- `manifest.json`
  - 全量结果，适合程序读取
- `manifest.csv`
  - 便于 Excel/表格查看（需 `--manifest-csv`）
- `summary.json`
  - 汇总统计、格式分布、最大输出项
- `failures.json`
  - 失败输入和错误信息（有失败时才生成）

## 微信同步监控

`sync-scan` 会读取你本机 `wechat-cli` 生成的：

- `~/.wechat-cli/config.json`
- `~/.wechat-cli/all_keys.json`

然后：

1. 解密 `emoticon.db`
2. 读取 `kFavEmoticonOrderTable`
3. 找出新同步到 PC 的表情
4. 可选下载 `cdn_url` 原图

### 示例

首次建立基线：

```powershell
python -m meme_cli.cli sync-scan sync_report_dir --baseline
```

后续扫描新增：

```powershell
python -m meme_cli.cli sync-scan sync_report_dir --download-cdn
```

调试某个区间：

```powershell
python -m meme_cli.cli sync-scan sync_report_dir --since-rowid 654 --download-cdn
```

### 监控输出

- `sync_report.json`
  - 新增表情列表、md5、rowid、cdn_url 等
- `sync_state.json`
  - 上次扫描到的 `last_fav_rowid`
- `exports/*.png`
  - 若指定 `--download-cdn`，会尝试下载明文图片

## 拼图切单图

`split-sheet` 适合把规则拼图切成单个表情：

- 纯白或单一浅色背景
- 明确的 `rows x cols`
- 格子之间有留白
- 每格内容不要跨格

### 示例

```powershell
python -m meme_cli.cli split-sheet `
  sheet.png `
  out_dir `
  --rows 3 --cols 3 `
  --size 200 `
  --format gif `
  --mode 2 `
  --transparent-bg
```

### 输出

- `1_r1c1.gif ...`
- `preview_boxes.png`
  - 标出切线和实际 trim 框
- `sheet_report.json`
  - 记录每块的原始格子范围和裁切范围

### 建议生图约束

- strict `3x3` / `4x4` regular grid
- pure white background
- equal spacing / wide gutters
- each sticker fully contained in its own cell
- text and decorations must stay inside each cell
- no overlap
- no UI / no screenshot overlays
