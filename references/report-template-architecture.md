# 报告JSON与现有渲染器

三分支独立，不混用JSON。所有附件相对于本轮报告目录，路径必须真实存在。新报告使用新目录，避免覆盖历史。

## 自己的作品

`review_analysis.py prepare --media-receipt <真实素材回执> --context <上下文JSON> --output <新报告目录>`生成fact_packet.json与report_data.json空模板。context包括title、report_label、snapshot_date、subject_role、account_profile_verified、metrics_source、metrics、facts及comments；未知值省略，facts/comments每项text与source，指标必须与该作品对应。

模型先填analysis_detail（purpose、data_reading、timeline、findings、coverage、visual_audit、plan），再填report_prose（version、summary、sections）。每个section有title、finding_refs、paragraphs，可用presentation与figures。完整字段和枚举由prepare生成的模板、review_analysis.validate与render_review_prose.validate校验，不编空内容绕过。调用顺序见report-rendering.md。

## 单平台周期

meta：report_type=account_cycle_review，is_synthetic_sample为布尔值；真实报告设false。account、period、coverage、summary、metrics、content、causes、actions、attachments均为对象。

period含start_date、end_date、days_requested及comparison；coverage含overall、daily_coverage（days_covered、days_missing、missing_dates）、metrics_coverage（metric_key、status），比较覆盖按实际填写。summary含非空headline、overview、next_direction，可补good_points/concern_points。metrics含overall，指标各有value、unit、note，未知value/unit为null；daily_series、comparison、work_accumulated按事实添加。content含posts_in_period、posts_note、groups。causes含screening_findings（observation、data_basis、confidence）、distribution_factors、deep_dives。actions含retain、adjust（what、bound_to_finding）、topic_suggestions、materials_needed及review（check_date、metrics、retain_if、adjust_if）。attachments的base_dir_relative='.'，items为path、description。

## 多平台周期

meta.report_type=multi_platform_cycle_review；meta.is_synthetic_sample同上。account、period、summary、cross_platform_comparison、platform_sections、causes、actions、data_boundaries、attachments均须存在。platform_sections为数组，每项platform、metrics_rows（每行[label,value,unit,note]）、可选series（dates、values、columns）、notes、comparison、works_top。跨平台comparable_fields每项metric、label、per_platform，其中值及header必须对应该平台实际指标行，不能互换指标。原始facts和渲染字段不同，由Agent逐字段映射并核数，不靠字段名猜测。

可选字段在代码中有更细枚举与校验。成稿前读取对应build_*脚本的validate和build_story，保证实际本版字段完整；这两处随包交付的程序是字段权威。模板通过只能证明结构，不能替代分析质量与图文检查。
