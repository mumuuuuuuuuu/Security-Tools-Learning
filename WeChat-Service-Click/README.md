# WeChat Service Click

一个面向 Windows 微信 PC 客户端的实验性信息收集辅助工具，用于批量搜索微信服务号，自动点击一级、二级菜单及微服务入口，并触发相关网络请求，方便抓包工具记录和分析流量。

该工具是我在学习信息收集流程时开发的，主要用于减少逐个搜索服务号、进入私信页面和手动点击菜单的重复操作。

> [!WARNING]
> 本项目目前处于学习和实验阶段。主体流程已在多台 Windows 电脑上测试，但 OCR 点击可能受到屏幕分辨率、显示缩放、微信窗口尺寸及客户端版本影响，部分环境可能出现坐标偏移。

## 使用场景

部分目标会通过微信服务号提供业务办理、在线查询、H5 页面、小程序等入口。当需要查看多个服务号的菜单结构时，逐个搜索和点击会占用较多时间。

用户只需把服务号名称写入 `accounts.txt`，程序就会按照顺序搜索服务号、进入私信页面，并尝试遍历菜单中的微服务入口。

本工具本身不负责抓包，只负责减少重复点击并产生访问流量。网络请求需要由用户提前配置的抓包工具记录和分析，不能完全替代人工判断。

> [!IMPORTANT]
> `accounts.txt` 中的服务号名称必须事先确认，并与微信中的完整名称保持一致。当前版本不会智能匹配搜索结果，而是直接点击第一个服务号结果；名称不准确时可能进入错误页面。

## 主要功能

- 从 `accounts.txt` 批量读取服务号名称
- 查找用户提前打开的微信“搜一搜”窗口
- 自动搜索并进入服务号私信页面
- 识别并点击一级、二级菜单
- 尝试打开菜单对应的 H5 或其他微服务入口
- 通过自动点击触发网络请求，辅助抓包分析
- 优先使用 Windows UI Automation 定位控件
- UI Automation 无法识别时使用 OCR 回退
- 保存调试截图和运行日志

## 实现方式

程序优先使用 `pywinauto` 和 `uiautomation` 获取微信窗口及控件位置。

部分微信界面或 H5 页面无法直接读取控件信息时，程序会使用 OpenCV 处理截图，通过 Tesseract OCR 识别文字位置，再使用 PyAutoGUI 执行点击。

```text
读取 accounts.txt
        ↓
查找“搜一搜”窗口
        ↓
搜索并进入服务号
        ↓
进入私信页面
        ↓
识别一级、二级菜单
        ↓
依次点击微服务入口
        ↓
处理下一个服务号
```

## 下载和使用

没有配置 Python 环境的用户，可以前往 [Releases](https://github.com/mumuuuuuuuuu/Security-Tools-Learning/releases/tag/v0.1.0) 下载：

```text
WeChatTool.zip
```

### 使用步骤

1. 完整解压 `WeChatTool.zip`
2. 打开 `accounts.txt`
3. 每行填写一个经过确认的服务号完整名称
4. 启动抓包工具并确认能够记录微信相关流量
5. 登录微信 PC 客户端
6. 提前打开微信“搜一搜”窗口
7. 双击 `WeChatTool.exe`
8. 运行期间不要移动、缩放或遮挡微信窗口

请保持以下目录结构，不要单独移动 EXE 或删除 `_internal`：

```text
WeChatTool/
├─ WeChatTool.exe
├─ accounts.txt
└─ _internal/
```

Release 版本已经包含 Python 运行环境、相关依赖和 Tesseract OCR，普通用户无需另外安装。

## accounts.txt 格式

```text
示例服务号A
示例服务号B
示例服务号C
```

- 每行填写一个服务号
- 名称必须与微信中的完整名称一致
- 不要添加多余空格
- 不要将包含真实收集目标的 `accounts.txt` 上传到公开仓库

## 源码运行

源码运行需要：

- Windows 10/11 64 位
- Python 3.13
- 微信 PC 客户端
- Tesseract OCR，以及 `chi_sim`、`eng` 语言包

安装依赖：

```powershell
python -m pip install -r requirements.txt
```

复制并修改服务号示例文件：

```powershell
Copy-Item "accounts.example.txt" "accounts.txt"
```

运行程序：

```powershell
python "scripts\wechat_open_service_chats.py"
```

如果 Tesseract 不在默认位置，可以手动指定：

```powershell
python "scripts\wechat_open_service_chats.py" "accounts.txt" --tesseract-cmd "C:\Program Files\Tesseract-OCR\tesseract.exe"
```

## 项目结构

```text
WeChat-Service-Click/
├─ scripts/
│  ├─ wechat_open_service_chats.py
│  └─ wechat_service_menu_runner.py
├─ accounts.example.txt
├─ requirements.txt
└─ README.md
```

## 已知问题

- OCR 点击在不同分辨率和显示缩放比例下可能发生偏移
- 移动或缩放微信窗口可能影响坐标转换
- 当前版本直接点击第一个搜索结果，不会判断相似服务号
- 微信客户端更新后，控件结构和按钮名称可能变化
- 网络较慢时，页面未加载完成可能导致流程失败
- 暂不适合无人值守的重要任务
- 不同微信版本或页面类型可能不会使用系统代理，抓包前需要先验证代理链路

出现问题时，可以查看程序生成的 `debug/` 截图及 JSONL 日志。

## 项目状态

当前版本：`v0.1.0 Experimental`

这是个人学习阶段编写的小工具，主要用于记录和展示 Windows 桌面自动化、OCR 识别及 Python 程序打包的实践过程，不保证在所有设备和微信版本中稳定运行。

## 免责声明

本项目仅用于技术学习、自动化方案研究及合法合规的信息收集辅助。请勿用于未经授权的信息收集、批量骚扰、恶意营销或其他违法违规活动。

使用者应遵守相关法律法规和微信平台规则，因不当使用造成的后果由使用者自行承担。