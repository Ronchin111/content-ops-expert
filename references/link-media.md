# 选定作品素材

只处理用户给定的作品，不搜索替代账号。`run_link_media.py`从stdin读取JSON（share_text、work_role=own/benchmark等真实用途标签），先plan，再在相同输入下按授权带execute；output必须是新目录。Agent读取脚本help完成参数，不让用户拼命令。

抖音/视频号解析现用https://sph.miuistore.com第三方服务；必须向用户说明会发送选定的公开分享链接并获授权后，才能带allow-parser-service。该第三方服务不保证持续可用；不得发送后台数据、Cookie、原片或账户秘密。平台限制就停止，不绕登录、频控或验证码。服务不可用时，接收用户已有原片/脚本继续范围内分析，不能把脚本称为原片证据。

视频处理按需安装av、Pillow、imageio-ffmpeg，转写另装faster-whisper及依赖。模型不随包交付；Agent从faster-whisper官方说明 https://github.com/SYSTRAN/faster-whisper 指向的模型来源核实并下载可用small模型到用户工作目录，核实际文件与空间，传model-dir。本版process_video_sample.py使用本地模型，不自动下载；没有模型不能声称已转写。断网先保留视频和截图，可跳过转写，标明缺口，之后复用原文件继续。

小红书素材需要Node、OpenCLI 2.0.3与官方Browser Bridge扩展；只有该分支准备。官方项目 https://github.com/jackwener/opencli ，扩展由官方项目指向的Chrome商店安装并由用户正常授权。Node从 https://nodejs.org/ 取得；Agent核版本后在本Skill的vendor/opencli-2.0.3下安装@jackwener/opencli@2.0.3，保持代码预期node_modules相对目录；该安装目录与第三方依赖未随包交付。node-path传实际位置，不照搬作者路径。读read_selected_media.mjs/read_open_note.mjs前确认Bridge上下文，扩展与版本兼容以实际连接为准；固定端口占用时解释限制，不抢占、不承诺任意端口替代。

取得视频后实际检查对应身份、机器时间稿和截图。基础五帧不是全片，不推断连续动作、语气或精确流失原因。链接失败时可用用户提供的脚本、原片、截图分别分析；明示各项依据能支持什么、缺什么，不补造转写、评论或后台数字。
