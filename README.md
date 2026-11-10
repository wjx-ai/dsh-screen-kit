# dsh-screen-kit

> Screen reading and mouse/keyboard control for DeepSeek Harness agents.
> Local OCR (no model call per step), precise text clicking, and batch clicking.
> Windows ✅ verified · macOS ⚠️ untested · Linux ⚠️ untested (X11 only, no Wayland input)

让 agent 真的能看屏幕、动鼠标。七个工具，全部走本地 OCR + 各平台原生输入接口，
**每一步都不经过大模型**。

## 为什么需要它

DSH 本身没有"手"。接一个 MCP 也能有，但那条路上每一步都要过一遍大模型 ——
在真实项目里实测每次视觉调用 20~57 秒，开一张发票要 25 秒以上。

这个插件把"看"和"点"做成纯本地动作：整批点击在一个 Python 进程里跑完，
**实测每张发票约 8 秒**，比走模型快 3 倍以上。

## 平台支持

| 平台 | 截图 | 鼠标/键盘 | 窗口 | 验证状态 |
|---|---|---|---|---|
| Windows 10/11 | GDI BitBlt (ctypes) | SendInput (ctypes) | ✅ | **已实测**（生产环境开出 90+ 张发票） |
| macOS | `screencapture`（系统自带） | Quartz CGEvent (pyobjc) | ✅ | **未实测**，见下方检查清单 |
| Linux X11 | `mss` / `grim` / ImageMagick / `scrot` | XTEST (ctypes) | ✅ (`wmctrl`/`xdotool`) | **未实测**，见下方检查清单 |
| Linux Wayland | ✅（`grim` / `mss`） | ❌ **不支持** | 部分 | Wayland 不允许程序注入输入事件 |

> **诚实说明**：开发者只有 Windows 机器。macOS 和 Linux 的实现是按要求写的，
> 逻辑与 Windows 版对应，但**没有在真机上跑过**。第一次在这些平台使用时，
> 请按下面的清单逐条确认 —— 不要直接拿去做批量操作。

### macOS 需要确认的
1. `python3 bridge.py info` 要能报出 `"backend": "macos"` 和正确的 `screen` 尺寸
2. **Retina 缩放**：`info` 里的 `scale` 是 2.0 时，`screen_click_text` 点得准不准
   （这是最容易错的地方，错了会偏一半）
3. 系统权限：屏幕录制 + 辅助功能都要给，否则表现是"操作成功但没反应"
4. 多显示器下 `screen_bounds()` 是否覆盖了所有屏

### Linux 需要确认的
1. `python3 bridge.py info` 要能报出 `"backend": "linux"` 和 `"session": "x11"`
2. 截图工具：装了哪个就走哪个（`mss` 优先）
3. 中文 OCR 是否正常（`screen_read` 读一段中文看看）
4. 多显示器下 XTEST 的坐标是否为整屏坐标（Xinerama 下是，独立 screen 下不是）

## 安装

### 1. Python 依赖

```bash
pip install rapidocr_onnxruntime Pillow numpy     # OCR 必需（自带中文模型）
pip install mss                                   # Linux 截图推荐
pip install pyobjc-framework-Quartz               # macOS 输入必需
```

Windows 不需要额外系统包（全走 ctypes）。

### 2. 挂进 DSH

把整个目录放进 profile 的 `plugins/`，然后在 `$DSH_HOME/profiles/<name>/cordis.patch.yml` 里：

```yaml
- insert:
    - id: screen-kit
      name: ./plugins/dsh-screen-kit/index.js
```

profile 默认 `patchReload: live`，存盘即热加载。

### 环境变量

| 变量 | 说明 |
|---|---|
| `DSH_SCREEN_KIT_PYTHON` | 指定 Python 解释器（默认找 `python3` / `python` / `py`） |
| `SCREEN_KIT_BACKEND` | 强制指定后端 `windows` / `macos` / `linux`（调试用） |

## 七个工具

| 工具 | 作用 |
|---|---|
| `screen_read` | 截图 OCR，返回每行文字的像素坐标和归一化坐标 |
| `screen_find` | 找一段文字，返回按"像不像按钮"排序的候选（不点击） |
| `screen_click` | 按像素坐标点击 |
| `screen_key` | 按功能键（esc/enter/tab/方向键）。**故意不支持打字** |
| `screen_click_text` | 找到文字并点它（点的是**那几个字自己的中心**） |
| `screen_batch_click` | 连续点同一套按钮，自带停止条件；`dry_run` 可只定位不点 |
| `screen_windows` | 列窗口 / 把某个窗口切到前台 |

## 三个关键设计（都是踩坑换来的）

### 1. 点的是"那几个字的中心"，不是"那一行的中心"

OCR 经常把一排相邻按钮读成一整块。真实例子：

```
块文字: '医保退费追溯码开票打印中'
块中心: 落在「追溯码」上      ← 点它会点到错误的按钮
```

所以保留 OCR 的**分段**（`segments`），找「开票」时算出这两个字自己的矩形，
点它的中心。这个问题在真实收费界面上点错过好几次。

### 2. 用"档位"判断这段字是不是一个独立控件

同一个屏幕上可能同时有：

| 文字 | 档位 | 含义 |
|---|---|---|
| `开票`（分段里独立的一段） | 0 | 真按钮 |
| `开票￥227.00`（在块开头） | 1 | 多半是按钮，但混了金额 |
| `挂号开票状态：待开票`（夹在中间） | 2 | 纯状态文字，点它什么也不会发生 |

只看文字长短会选错：金额前缀出现后，按钮那一行变成
`¥227.00 ?医保退费追溯码开票打印中`（21 字），会输给 9 字的 `挂号开票状态：待开票`。
**必须按档位排，不能按长度排。**

### 3. 判断弹窗出现/关闭要用 OCR，不能用画面差分

踩过的坑：用"画面变化超过阈值"判断弹窗开了。结果列表重绘、光标闪烁也会触发，
于是在弹窗还没渲染完时就去 OCR，查不到按钮，误判成"弹窗里没有按钮"而停住。

**差分只能说明"画面变了"，不能说明"按钮在了"。**

## 性能：区域大小决定一切

Windows 1920×1080 / DPI 125% 实测：

| 操作 | 耗时 |
|---|---|
| 截图 | 0.05 秒 |
| 小区域 OCR（如 576×432） | 0.7 秒 |
| **全屏 OCR（1920×1080）** | **13 秒** |

所以：**能用 `region` 就一定用**。`screen_batch_click` 必须给紧的 `top_region`
和 `popup_region` —— 这也是区分"工具栏里的按钮"和"弹窗里的按钮"的唯一办法。

## 用法

先看清楚屏幕，再动手：

```
screen_read      region="0,0,1000,280"
screen_find      text="开票" region="0,0,1000,280"
```

确认坐标对了，批量干：

```
screen_batch_click
  top_text     = "开票"
  top_region   = "0,0,1000,280"        # 工具栏那个
  popup_region = "700,600,1000,1000"   # 弹窗那个
  interval     = 2                      # 每次点击间隔, 给界面反应时间
  times        = 30
  empty_text   = "暂无可开发票"          # 撞到这个就停
```

不确定区域对不对就先 `dry_run=true`，它只报坐标不点击。

### 停止条件

`screen_batch_click` 自己会在这些情况下停下，并说清是哪一种：

- 出现了 `empty_text`（例如"暂无可开发票"），并且会把那个提示框按 Esc 关掉
- 点了起始按钮但弹窗按钮一直不出现（重试一次后）
- 点了弹窗按钮但弹窗关不掉
- 达到 `times` 上限

## 自检

```bash
# 单元测试: 脱屏, 任何平台都能跑
python -m unittest discover -s tests -t .

# 环境自检: 报告后端、能力、屏幕尺寸、OCR 引擎
python bridge.py info

# 插件自检: 用 mock ctx 加载 index.js, 检查七个工具都注册上, 跑两个只读调用
node test-plugin.mjs
```

## 已知限制

- **只管屏幕像素。** 没有无障碍树（AX/UIA），所以不认识"这是个按钮"，
  只认识"这几个字在这一行里"。复杂界面建议先 `screen_read` 看清结构。
- **`screen_batch_click` 的成功判据是"弹窗关掉了"**，不是"业务系统真的入账了"。
  如果应用关掉弹窗却报错，它看不出来 —— 重要批量任务请抽查结果。
- **`screen_key` 不负责打字。** 只做几个功能键，因为"往任意窗口输入任意文字"
  是公用插件不该默认拿到的权限。
- **Wayland 下不能点鼠标。** 这是 Wayland 的安全模型，不绕过。
- **中文识别靠 rapidocr**，字体太小、对比度太低会漏字。
  漏了就 `screen_read` 看看实际读到什么。
- **不处理 UAC / 权限弹窗。** Windows 的安全桌面截不到也点不了，这是设计如此。

## 安全边界

这个插件能移动鼠标、点按钮、按功能键 —— 请自己把握授权范围：

- 没有"打任意文字"的能力（`screen_key` 只有功能键白名单）
- 没有"读剪贴板"的能力
- 不自动确认权限/安装/UAC 弹窗
- 所有坐标都来自**当前这一帧截图**，不写死

## OCR 层的来源

`screen/ocr.py` 里的分段定位、合并规则、模糊匹配分级，来自
[fishAI](https://github.com/wjx-ai) 的 OCR 层 —— 那部分是在真实业务界面上
反复点错之后修出来的。搬过来是为了不让两个项目各自踩一遍同样的坑。

## License

MIT