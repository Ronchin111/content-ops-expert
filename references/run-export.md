# 随包主工具的调用

工具根=`<Skill根>/assets/media-export-tool`。Agent先确认该副本不在维护、服务使用的Python一致、没有其他任务运行。服务地址为`http://127.0.0.1:8766`，端口占用且身份未知就解释占用情况，不抢占或杀进程。

服务未运行时，在工具根用已准备的独立Python运行`launcher.py`，子进程环境设`MEDIAEXPORT_NO_PANEL_PAGE=1`，保存启动日志。服务正常才继续；启动失败读本次日志，具体区分解释器、缺包、浏览器、端口和权限错误。

用户已要求导出即构成该范围任务授权；Agent代入技术参数。单平台`scripts/run_export.py`，多平台`scripts/run_batch.py`，先读脚本帮助，plan后核对实际计划，使用execute与计划实际sha256执行。参数包括tool-root、panel、platforms、account-alias、save-to、plan-out，具体以对应帮助为准。哈希仅为程序内绑定，不要求用户手工抄写。新输出目录的父目录先建立，计划文件放在批次之外。

登录由平台脚本完成。用户只需在提示时扫码、验证码或正常授权；不读取/展示Cookie，不关闭安全防护。账号代号不是身份凭据，核对页面账号后绑定报表。扫码超时保留本轮状态，在确认任务已结束后，仅重试失败平台。未知是否提交成功时不重发。

读取本轮batch-summary.json、feedback.md及result.json，成功/正常无数据/失败分别说明。只在实际平台空态时认定无数据，空文件或超时不是无数据。多平台部分失败保留成功项与原回执；恢复见report-retry-current.md。
