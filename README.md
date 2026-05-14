# 表情包工坊

本地表情包和小红书组图制作工具。核心目标是**傻瓜式操作**：拖入图片、选择目录、点击开始，自动切图、组图、加字幕、预览发布效果。

## 功能概览

- **九宫格切图**
  - 支持 3x3、4x4、5x5 网格预设
  - 支持 `png/jpg/jpeg/bmp/gif/webp`
  - 自动输出单张切片、预览框图和切图报告

- **转换器**
  - 批量转换图片
  - 支持 GIF / PNG 输出
  - 支持微信表情常用尺寸和背景转透明

- **组图器**
  - 小红书上下拼接模板
  - 多张图片按顺序两两组合
  - 支持选择“九宫格切图结果目录”
  - 支持 `xhs_plan.json` / `captions.txt` 自动加字幕
  - 支持小红书发布预览

- **输出目录**
  - 自动收集最近切图和组图结果
  - 以缩略图卡片展示结果目录
  - 点击整条卡片可打开目录
  - 切图目录可一键放入组图器

- **小红书插图 Skill**
  - 项目内置一份 Codex skill：
    ```text
    skills/xhs-life-philosophy-illustration
    ```
  - 可根据主题生成：
    - 标题钩子
    - 固定小红书标签
    - 9 条文案切片
    - 无字九宫格生图提示词
    - 九张单图提示词
    - 可供组图器读取的 `xhs_plan.json`

## GUI 启动

```powershell
python run_gui.py
```

## 常用工作流

### 1. 九宫格图 → 切图 → 小红书上下组图

1. 打开 **九宫格切图**
2. 选择或拖入一张九宫格图片
3. 点击开始切图
4. 打开 **组图器**
5. 点击 **选择切图结果目录**
6. 在弹出的可视化窗口中选择刚刚的切图结果
7. 可选：选择文案文件 `xhs_plan.json` 或 `captions.txt`
8. 点击开始组图

### 2. 使用内置 Skill 生成小红书文案包

示例：生成“松弛感自救 + 打工猫”素材包。

```powershell
python .\skills\xhs-life-philosophy-illustration\scripts\create_xhs_project.py `
  --theme "松弛感自救" `
  --style office-cat `
  --hook "瞬间被这段话点醒了~" `
  --output .\xhs_project
```

输出内容：

```text
xhs_project/
  hook.txt
  tags.txt
  post_copy.txt
  captions.txt
  xhs_plan.json
  image_prompt_grid.txt
  image_prompt_individual.txt
```

其中：

- `hook.txt`：标题钩子
- `tags.txt`：固定小红书标签
- `post_copy.txt`：标题钩子 + 标签，可直接复制发布
- `xhs_plan.json`：包含 captions、hook、tags，可直接给组图器当“文案文件”

推荐流程：

1. 用 `image_prompt_grid.txt` 生成一张**无字九宫格图**
2. 在表情包工坊里切图
3. 在组图器里选择切图结果目录
4. 文案文件选择 `xhs_project/xhs_plan.json`
5. 开始组图
6. 发布时可复制 `xhs_project/post_copy.txt`

Skill 当前固定内容方向：治愈、拒绝焦虑、自我成长。

## 内置主体样式

Skill 当前内置这些主体：

| ID | 说明 |
|---|---|
| `workplace-monk` | 职场和尚，适合职场低内耗、加班边界 |
| `office-cat` | 打工猫，适合松弛感、自我照顾、打工人共鸣 |
| `round-office-worker` | 圆脸打工人，适合泛职场主题 |
| `zen-rabbit` | 禅意兔子，适合人生哲学、情绪稳定 |
| `tiny-robot` | 小机器人，适合 AI、效率、信息过载 |

## CLI 示例

### 切图

```powershell
python -m meme_cli.cli split-sheet sheet.png out --rows 3 --cols 3 --size raw --format png
```

### 组图并加字幕

```powershell
python -m meme_cli.cli stitch-vertical tiles groups `
  --xhs-plan .\xhs_project\xhs_plan.json `
  --cell-size 1080 `
  --gutter 16
```

### 只用 captions.txt 加字幕

```powershell
python -m meme_cli.cli stitch-vertical tiles groups `
  --captions .\xhs_project\captions.txt `
  --caption-height 180 `
  --caption-font-size 64
```

### 批量转换

```powershell
python -m meme_cli.cli convert input_dir output_dir --mode 2 --size 200
```

## 打包 EXE

```powershell
.\build_gui_exe.bat
```

打包后会生成：

```text
dist/表情包工坊.exe
```

## 项目结构

```text
meme-cli/
  meme_cli/
    cli.py          # CLI 与图片处理核心逻辑
    gui.py          # Tkinter GUI
  skills/
    xhs-life-philosophy-illustration/
      SKILL.md
      scripts/create_xhs_project.py
      references/
  assets/
  run_gui.py
  build_gui_exe.bat
```

## 开发检查

```powershell
python -m py_compile meme_cli\cli.py meme_cli\gui.py
python -m py_compile skills\xhs-life-philosophy-illustration\scripts\create_xhs_project.py
```
