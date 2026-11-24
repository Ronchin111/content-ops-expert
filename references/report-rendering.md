# 报告生成与检查

报告依赖：reportlab、Pillow、charset-normalizer，Excel事实汇总另用openpyxl、tzdata。Windows系统字体按首次准备说明检查。不得复制作者字体或借作者专用运行时路径冒称消费者已有。

自己作品：先按report-template-architecture.md生成真实fact_packet和空report_data；填完分析后，用同一Python运行`review_analysis.py validate <report_data.json>`。通过后运行`build_report_pdf.py <report_data.json>`，得到作品复盘报告.pdf和同文.md。新正文按report_prose自然分页，保留原句/时间/画面的依据和改法。无可用音轨时不能假造prepare所需回执；可按已提供脚本和截图先做范围明确的文字报告。

周期：填好对应JSON，运行`build_cycle_report_pdf.py <json>`或`build_multi_cycle_report_pdf.py <json>`。对标：按benchmark-report-template.md填独立JSON，运行`build_benchmark_pdf.py <json>`。对标渲染器可覆盖同名结果，Agent必须用新任务目录。

渲染后确认文件实际存在且非空，查看PDF全部页面，检查中文字体、截图与图注、断页和数字。宿主无法看PDF时至少核Markdown，明确PDF视觉检查未完成。PDF失败保留JSON和Markdown，具体解释缺包、字体或字段错误；修复后接续，不重取已经成功的数据。不能把机器稿、静帧或字段校验称为完整连续视频分析或运营效果证明。
