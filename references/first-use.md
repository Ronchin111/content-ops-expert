# 首次准备：由Agent完成技术操作

1. 以SKILL.md所在目录为Skill根，工具根固定为其下`assets/media-export-tool`。检查launcher.py、requirements.txt和scripts存在。不要用作者机器路径。选择本地可写工作目录保存环境和任务；避免在将被平台替换的Skill目录内保存长期报告、模型和登录资料。
2. 检查Windows 11 x64及宿主的文件读写、终端、联网能力。无这些能力时说明具体不支持项，不能把纯聊天窗口当本地Agent。用户只需确认首次准备和宿主的正常授权。
3. 检查可信Python 3.10+（建议普通CPython 3.12 x64；不使用WindowsApps占位程序），Chrome及所选功能依赖。解释器候选要核实际可运行版本，复用已满足要求的独立环境，不改系统PATH、不覆盖用户其他环境。
4. 无Python：Agent打开Python官方Windows下载入口 https://www.python.org/downloads/windows/ ，核实真实可下载的安装包及发布信息后，按宿主权限在用户目录安装。Windows自带PowerShell/宿主下载能力即可取得安装器，不要求事先有Python、Node或PS7。程序/协议/安全提示需要本人处理时解释当前一步，不让用户找命令，不绕过警告。当前包没有实现无人值守的Python下载器；不能声称EXE会完成这些操作。
5. 有Python后，Agent用该解释器在工作目录建立venv，再用该环境的`python -m pip`安装所需依赖。报表导出读主工具requirements.txt；报告安装reportlab、Pillow、charset-normalizer、openpyxl、tzdata。只做报表不安装Node、语音模型。复检各包可导入，记录解释器位置供下次复用。
6. 缺Chrome：由Agent查Google官方Chrome下载页 https://www.google.com/chrome/ ，根据正常用户级安装权限协助；本版不把Edge识别为Chrome已验证支持。已有浏览器先用原版本，不关闭安全防护或强行降级。已有浏览器不表示登录有效。
7. 视频处理才安装av、Pillow、imageio-ffmpeg；转写才安装faster-whisper及其依赖、下载可用本地模型。模型见素材说明。XHS素材才准备Node、OpenCLI及官方扩展，账户报表导出不需要这项。
8. 下载前检查目标盘空间，先说明本次实际包体积/模型大小（未知就说明未知），不可把本ZIP大小说成全部下载量。下载失败保留缓存和已安装部分，下次从缺项继续；pip可重用缓存，但不承诺所有源均支持断点续传。失败时报告下载对象、来源、网络/权限/空间的具体错误，不默认重装全部。

## 启动之前核实

在所选解释器里验证playwright、openpyxl、httpx；报告另验证排版依赖及Windows字体msyh.ttc、msyhbd.ttc、seguiemj.ttf（系统字体不随包分发）。用该解释器启动随包launcher.py，确认服务与工具目录匹配、/api/env及空闲状态正常，再执行用户任务。安装命令退出0不等于导出成功。

保留PrepareEnvironment.exe、native-bootstrap源码及旧PS脚本供已有安装候选复用；它们可能要求PS7或本地安装资产，不能作为本版默认自动下载入口，也不能放宽执行策略。用户电脑上的准备按实际授权进行；文件下载完成不代表本地环境已经准备成功。
