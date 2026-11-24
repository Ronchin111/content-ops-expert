# 周期报告：事实与成稿分开

明确账号、平台、日期和目标。缺事前目标就描述实际结果，不倒填达成率。数据按所属平台与导出时间说明；账号周期发生量与期内作品截至快照日的累计表现分开。未知单元格不填零，比例不相加，跨平台不拼总成绩。

抖音先准备source-binding.json：`{"platform":"抖音","account_name":"实际核对的账号","basis":"本次页面/报表身份依据","collected_at":"实际时间","sources":[{"path":"实际绝对文件路径","sha256":"Agent计算的实际摘要"}]}`。只绑定本次用户明确材料，不能凭文件名认定账号。

用`summarize_account_cycle.py --source-binding ... --daily ... --works ... --snapshot-date YYYY-MM-DD --period previous_week --output <新目录>`整理事实；daily/works按实际取得项传入。可用last7/last30/previous_month/custom。无可比期时加skip-comparison，不伪造比较。

多平台用`summarize_multi_platform.py`：读取platform-bindings（顶层platforms数组），各项源文件与绑定结构以该脚本run_douyin/read_xhs_platform/read_wechat_platform的实际字段为准。小红书/视频号须显式custom日期，不沿用作者样例的时间窗口。没有某平台数据就按本次原因写缺口。

宿主模型根据事实写summary、causes、actions，保留来源与限制，按回顾目标、现状表现、原因、下期行动组织。先完成可读Markdown；PDF用单平台build_cycle_report_pdf.py或多平台build_multi_cycle_report_pdf.py。字段见report-template-architecture.md；不要把汇总facts原样当成最终报告JSON。生成前后核数值和时间，截图只能支持实际可见内容。
