# 导出失败后的处理

先读实际batch-summary.json及各平台result/feedback/log，保留成功项、NO_DATA项和所有原始回执。单个平台失败不全量重导，结束状态未知不重发。确认失败项可重试后，用run_batch.py plan --resume-from <原批次> --save-to <新批次> --plan-out <批次外新计划>，按实际需要加retry-failed；读取该版本help核参数。

旧integrations/report-retry是按需报表级适配候选，不自动覆盖随包主工具。需要应用时先核接口和版本、备份原副本、只处理明确失败报表；未适配时用平台级恢复并说明范围，不能宣称旧补丁已生效。
