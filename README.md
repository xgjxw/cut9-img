# cut9-img

九宫格切图器。支持把一张规则 3x3 / 4x4 / 5x5 拼图切成单张表情图，并自动打开输出目录。

## 功能

- GUI 傻瓜式操作：选择拼图或拖拽 / Ctrl+V 粘贴后点击开始切图
- 默认输出到临时目录，切图完成后自动打开
- 支持 `png/jpg/jpeg/bmp/gif/webp`
- 支持 3x3、4x4、5x5 网格预设，也可以手动设置行列
- 输出 `gif` 或 `png`
- 可选背景转透明
- 保留 CLI 能力：批量转换、拼图切图、微信表情同步扫描

## GUI 启动

```powershell
python run_gui.py
```

## 打包 EXE

```powershell
.\build_gui_exe.bat
Copy-Item .\dist\meme-gui.exe .\dist\九宫格切图器.exe -Force
```

## CLI 示例

```powershell
python -m meme_cli.cli split-sheet sheet.png out --rows 3 --cols 3 --size 200 --format gif --transparent-bg
python -m meme_cli.cli convert input_dir output_dir --mode 2 --size 200
```

## 开发

```powershell
python -m py_compile meme_cli\cli.py meme_cli\gui.py
```
